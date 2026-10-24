"""Retrieval contract, cache integrity, and cloud protocol regression tests."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from supplylens.retrieval import CHUNK_OVERLAP, CHUNK_SIZE, EmbeddingError, SOPIndex


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "sop"
    source.mkdir()
    (source / "01_overdue.md").write_text(
        "# 逾期未收货\n自编模拟 SOP。\n\n## 到货处理\n"
        "逾期未收货的订单应核对承诺日期、收货数量和供应商交付计划。", encoding="utf-8"
    )
    (source / "02_logistics.md").write_text(
        "# 物流异常\n自编模拟 SOP。\n\n## 运输中断\n"
        "物流异常需要向承运人确认运输中断原因和恢复时间，引用最新物流事件。", encoding="utf-8"
    )
    return SimpleNamespace(
        sop_dir=source,
        index_dir=tmp_path / "index",
        embedding_mode="local",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
        model_base_url="https://example.test/compatible-mode/v1",
        model_api_key="",
        request_timeout_seconds=3,
    )


def test_local_retrieval_citations_and_top_k(settings):
    index = SOPIndex(settings)
    build = index.build()
    assert build["mode"] == "local"
    assert build["embedding_model"] == "lexical-char-ngram-hash"
    assert build["documents"] == 2
    assert build["requires_lexical_anchor"] is True
    hits = index.search("物流异常运输中断", top_k=1)
    assert len(hits) == 1
    assert hits[0]["document"] == "02_logistics.md"
    assert "运输中断" in hits[0]["text"]
    assert hits[0]["source_type"] == "sop"
    assert hits[0]["id"].startswith("sop:")
    assert 0 < hits[0]["score"] <= 1.000001
    assert index.search("物流异常运输中断") == SOPIndex(settings).search("物流异常运输中断")


@pytest.mark.parametrize("query", ["", " \n\t", "？？！", None, 12, "xylophone nebula", "烹饪火锅菜谱"])
def test_invalid_or_unrelated_queries_return_empty(settings, query):
    assert SOPIndex(settings).search(query) == []


@pytest.mark.parametrize("top_k", [0, -1, True, "3", None])
def test_invalid_top_k(settings, top_k):
    assert SOPIndex(settings).search("逾期未收货", top_k=top_k) == []


def test_hash_collisions_do_not_create_unrelated_citations(settings):
    settings.embedding_dimensions = 1  # Every term collides, so cosine alone would match everything.
    index = SOPIndex(settings)
    index.build()
    assert index.search("烹饪火锅菜谱") == []


def test_chunk_boundaries_and_heading_stable_ids(settings):
    body = "# 到货异常\n## 重复标题\n" + "甲乙丙丁戊己庚辛" * 150 + "\n## 重复标题\n物流异常记录。"
    chunks = SOPIndex._split_document("rule.md", body.encode())
    main = [chunk for chunk in chunks if len(chunk["text"]) > 100]
    assert len(main) >= 3
    assert all(len(chunk["text"]) <= CHUNK_SIZE for chunk in chunks)
    for previous, following in pairwise(main):
        assert previous["text"][-CHUNK_OVERLAP:] == following["text"][:CHUNK_OVERLAP]
    assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    edited = SOPIndex._split_document("rule.md", body.replace("物流异常记录", "运输异常记录").encode())
    assert [chunk["id"] for chunk in edited] == [chunk["id"] for chunk in chunks]


def test_cache_reuses_vectors_without_embedding(settings, monkeypatch):
    original = SOPIndex(settings).build()
    fresh = SOPIndex(settings)
    monkeypatch.setattr(fresh, "_embed", lambda _: pytest.fail("valid cache should not re-embed"))
    result = fresh.build()
    assert result["cache_hit"] is True
    assert result["fingerprint"] == original["fingerprint"]
    metadata = json.loads((settings.index_dir / "index.json").read_text(encoding="utf-8"))
    matrix = np.load(settings.index_dir / metadata["vectors_file"], allow_pickle=False)
    assert matrix.shape == (result["chunks"], settings.embedding_dimensions)


def test_content_changes_rebuild_with_stable_citations(settings):
    index = SOPIndex(settings)
    initial = index.build()
    before = index.search("逾期未收货", top_k=1)[0]["id"]
    path = settings.sop_dir / "01_overdue.md"
    path.write_text(path.read_text(encoding="utf-8") + "另请确认运单。", encoding="utf-8")
    rebuilt = index.build()
    assert rebuilt["cache_hit"] is False
    assert rebuilt["fingerprint"] != initial["fingerprint"]
    assert index.search("逾期未收货", top_k=1)[0]["id"] == before


@pytest.mark.parametrize("field,value", [
    ("embedding_model", "other-model"),
    ("model_base_url", "https://different.test/v1"),
    ("embedding_dimensions", 64),
    ("embedding_mode", "cloud"),
])
def test_settings_invalidate_cache(settings, monkeypatch, field, value):
    initial = SOPIndex(settings).build()
    setattr(settings, field, value)
    settings.model_api_key = "test-only"
    fresh = SOPIndex(settings)
    calls = []

    def embed(texts):
        calls.append(texts)
        return fresh._local_embeddings(texts)

    monkeypatch.setattr(fresh, "_embed", embed)
    rebuilt = fresh.build()
    assert rebuilt["cache_hit"] is False
    assert rebuilt["fingerprint"] != initial["fingerprint"]
    assert len(calls) == 1


@pytest.mark.parametrize("corruption", ["truncated_json", "metadata_order", "shape", "checksum", "nonfinite", "path_traversal"])
def test_corrupt_cache_is_rebuilt_without_misaligned_evidence(settings, corruption):
    index = SOPIndex(settings)
    index.build()
    manifest_path = settings.index_dir / "index.json"
    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    matrix_path = settings.index_dir / metadata["vectors_file"]
    if corruption == "truncated_json":
        manifest_path.write_text('{"chunks":', encoding="utf-8")
    else:
        if corruption == "metadata_order":
            metadata["chunks"].reverse()
        elif corruption == "shape":
            np.save(matrix_path, np.zeros((1, 3), dtype=np.float32), allow_pickle=False)
            metadata["vectors_sha256"] = hashlib.sha256(matrix_path.read_bytes()).hexdigest()
        elif corruption == "checksum":
            matrix_path.write_bytes(b"not a numpy array")
        elif corruption == "nonfinite":
            vectors = np.load(matrix_path, allow_pickle=False)
            vectors[0, 0] = np.nan
            np.save(matrix_path, vectors, allow_pickle=False)
            metadata["vectors_sha256"] = hashlib.sha256(matrix_path.read_bytes()).hexdigest()
        else:
            metadata["vectors_file"] = "../outside.npy"
        manifest_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    fresh = SOPIndex(settings)
    assert fresh.build()["cache_hit"] is False
    assert fresh.search("物流异常运输中断", top_k=1)[0]["document"] == "02_logistics.md"


def test_atomic_manifest_survives_failed_publication(settings, monkeypatch):
    index = SOPIndex(settings)
    index.build()
    manifest = settings.index_dir / "index.json"
    original = manifest.read_bytes()
    source = settings.sop_dir / "01_overdue.md"
    original_source = source.read_bytes()
    source.write_bytes(original_source + "\n更新备注。".encode())
    from supplylens import retrieval

    replace = retrieval.os.replace

    def fail_manifest(src, dst):
        if Path(dst).name == "index.json":
            raise OSError("simulated interrupted publication")
        return replace(src, dst)

    monkeypatch.setattr(retrieval.os, "replace", fail_manifest)
    with pytest.raises(OSError, match="interrupted"):
        index.build()
    assert manifest.read_bytes() == original
    assert not list(settings.index_dir.glob("*.tmp"))
    source.write_bytes(original_source)
    assert SOPIndex(settings).build()["cache_hit"] is True


def test_concurrent_build_only_embeds_once(settings, monkeypatch):
    index = SOPIndex(settings)
    original = index._embed
    calls = []

    def embed(texts):
        calls.append(1)
        return original(texts)

    monkeypatch.setattr(index, "_embed", embed)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: index.build(), range(8)))
    assert len(calls) == 1
    assert len({result["fingerprint"] for result in results}) == 1
    assert sum(not result["cache_hit"] for result in results) == 1


def mock_client(monkeypatch, handler):
    client_class = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=transport, **kwargs))


def test_cloud_batches_and_reorders_response_indices(settings, monkeypatch):
    settings.embedding_mode = "cloud"
    settings.model_api_key = "unit-test-key"
    settings.embedding_dimensions = 4
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == "https://example.test/compatible-mode/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        assert payload["model"] == "text-embedding-v4"
        assert payload["dimensions"] == 4
        assert payload["encoding_format"] == "float"
        data = [{"index": i, "embedding": [float(text), 1, 0, 0]} for i, text in enumerate(payload["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    mock_client(monkeypatch, handler)
    index = SOPIndex(settings)
    vectors = index._cloud_embeddings([str(i) for i in range(1, 12)])
    assert [len(request["input"]) for request in requests] == [10, 1]
    assert vectors.shape == (11, 4)
    assert np.allclose(vectors[:, 0] / vectors[:, 1], np.arange(1, 12))
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1)


@pytest.mark.parametrize("response", [
    {"data": []},
    {"data": [{"index": 1, "embedding": [1, 0, 0, 0]}]},
    {"data": [{"index": False, "embedding": [1, 0, 0, 0]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}]},
    {"data": [{"index": 0, "embedding": [0, 0, 0, 0]}]},
    {"error": "invalid request"},
])
def test_cloud_invalid_response_never_falls_back(settings, monkeypatch, response):
    settings.embedding_mode = "cloud"
    settings.model_api_key = "test-only"
    settings.embedding_dimensions = 4
    mock_client(monkeypatch, lambda _: httpx.Response(200, json=response))
    index = SOPIndex(settings)
    monkeypatch.setattr(index, "_local_embeddings", lambda _: pytest.fail("silent fallback"))
    with pytest.raises(EmbeddingError, match="no local fallback"):
        index._embed(["物流异常"])


def test_cloud_transport_and_http_errors_are_explicit(settings, monkeypatch):
    settings.embedding_mode = "cloud"
    settings.model_api_key = "test-secret"
    mock_client(monkeypatch, lambda _: httpx.Response(401, text="a provider response containing test-secret"))
    index = SOPIndex(settings)
    monkeypatch.setattr(index, "_local_embeddings", lambda _: pytest.fail("silent fallback"))
    with pytest.raises(EmbeddingError, match="HTTP 401") as error:
        index.build()
    assert "test-secret" not in str(error.value)
    assert not settings.index_dir.exists()


def test_cloud_requires_key(settings):
    settings.embedding_mode = "cloud"
    with pytest.raises(EmbeddingError, match="MODEL_API_KEY"):
        SOPIndex(settings).build()


def test_cloud_timeout_is_not_hidden(settings, monkeypatch):
    settings.embedding_mode = "cloud"
    settings.model_api_key = "test-only"

    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    mock_client(monkeypatch, handler)
    index = SOPIndex(settings)
    monkeypatch.setattr(index, "_local_embeddings", lambda _: pytest.fail("silent fallback"))
    with pytest.raises(EmbeddingError, match="ReadTimeout"):
        index.build()


def test_cloud_unrelated_query_does_not_call_query_embedding(settings, monkeypatch):
    settings.embedding_mode = "cloud"
    settings.model_api_key = "test-only"
    index = SOPIndex(settings)
    monkeypatch.setattr(index, "_cloud_embeddings", index._local_embeddings)
    index.build()
    monkeypatch.setattr(index, "_cloud_embeddings", lambda _: pytest.fail("no lexical signal"))
    assert index.search("烹饪火锅菜谱") == []


def test_missing_source_does_not_create_empty_index(settings):
    settings.sop_dir = settings.sop_dir / "does-not-exist"
    with pytest.raises(ValueError, match="No Markdown"):
        SOPIndex(settings).build()


def test_all_eight_bundled_sops_are_explicitly_synthetic(settings):
    settings.sop_dir = Path(__file__).resolve().parents[1] / "data" / "sop"
    files = list(settings.sop_dir.glob("*.md"))
    assert len(files) == 8
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "自编模拟 SOP" in text
        assert "业务规则" in text
        assert "建议" in text
    index = SOPIndex(settings)
    assert index.build()["documents"] == 8
    for query in ["逾期未收货", "ETA 晚于承诺日期", "物流异常", "部分收货", "ETA 缺失", "状态陈旧", "数据一致性", "供应商跟进"]:
        assert index.search(query), query
