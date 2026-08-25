# DeepDoc Server (standalone) — build & run

Replaces the previous `infiniflow/ragflow:latest` image with a slim
container that runs **only** the DeepDoc Server module — no MySQL,
Elasticsearch, MinIO, or Redis required.

## Why

The full RAGFlow image starts a Flask + nginx + task-executor stack that
needs four backing services to even boot. Our design intent (CONSENSUS
phase1-mvp §248, DESIGN §887) is to use DeepDoc **independently** as a
document-parsing sidecar. This image fulfils that intent.

## Build

```bash
docker build -f infra/deepdoc/Dockerfile_deepdoc_oss \
             -t deepdoc_oss:latest \
             infra/deepdoc
```

The Dockerfile clones the `deepdoc/` subtree of
[`infiniflow/ragflow`](https://github.com/infiniflow/ragflow/tree/main/deepdoc)
and installs the Python dependencies it needs (LitServe + ONNX Runtime +
Pillow + Hugging Face Hub + PyMuPDF).

## Run standalone (smoke test)

```bash
docker run --rm -p 9390:9390 deepdoc_oss:latest

# In another shell:
curl http://localhost:9390/health
# -> ok

curl http://localhost:9390/model
# -> {"model":"oss","version":"1.0"}

curl -X POST http://localhost:9390/predict/ocr \
     -F "request=@some-page.jpg" \
     -F "operator=rec"
```

## HTTP contract

| Method | Path           | Body                                  | Response                              |
|--------|----------------|---------------------------------------|---------------------------------------|
| GET    | `/health`      | —                                     | `200 ok`                              |
| GET    | `/model`       | —                                     | `{"model":"oss","version":"1.0"}`     |
| POST   | `/predict/dla` | multipart `request=<JPEG>`            | `{"boxes":[{"type","bbox"}, ...]}`    |
| POST   | `/predict/ocr` | multipart `request=<JPEG>`, `operator=rec\|det` | `{"text": "..."}`             |
| POST   | `/predict/tsr` | multipart `request=<JPEG>`            | `{"html": "..."}` (table HTML)        |

`bbox` is `[x0, y0, x1, y1]` in pixel coordinates of the input image.

## Resource notes

- Image size: ~850-900 MB (Python 3.11-slim + ONNX Runtime + RAGFlow
  `deepdoc/` subtree + ONNX models baked into `/app/rag/res/deepdoc`)
- **Stub strategy** (mirrors RAGFlow's official `Dockerfile_deepdoc_oss`,
  lines 52-53): RAGFlow ships `deepdoc/server/docker_stubs.py` which
  generates minimal replacements for `deepdoc/__init__.py`,
  `deepdoc/vision/__init__.py`, `common/{__init__,file_utils,misc_utils}.py`,
  `rag/{__init__}.py`, `rag/nlp/__init__.py`, `rag/utils/lazy_image.py`
  under `/app`. These stubs break the heavy full-stack import chain
  (ruamel.yaml, elasticsearch, infinity, azure, gcs, minio, s3, oss,
  redis, chardet, cn2an, ...) at exactly the points the ONNX-only server
  touches. Result: no `common/` or `rag/` clone, no SDK installs.
- Source subtree included in the image (via `git sparse-checkout set
  deepdoc`):
  - `deepdoc/` — DLA / OCR / TSR endpoints + adapters + vision
    recognizers + `docker_stubs.py` + `download_deps.py`
- ONNX models (`layout.onnx`, `det.onnx`, `rec.onnx`, `tsr.onnx`,
  `ocr.res`) are baked into `/app/rag/res/deepdoc` at build time via
  `deepdoc.server.download_deps` (sources from
  [`InfiniFlow/deepdoc`](https://huggingface.co/InfiniFlow/deepdoc) on
  HuggingFace). `/predict/{dla,ocr,tsr}` is hot on first request — no
  startup wait for HuggingFace download. The path MUST match the
  `OCR.__init__` resolver (`get_project_base_directory() + "rag/res/deepdoc"`),
  otherwise the class falls back to `huggingface_hub.snapshot_download`
  and blocks the LitServe event loop on first boot. Override the model
  path via `CMD --model-dir /app/rag/res/deepdoc` if you move the cache.
- CPU inference is fine for typical workloads (~1-2 s per page). For
  GPU acceleration, swap `onnxruntime` for `onnxruntime-gpu` and ensure
  the host has CUDA 12.x drivers; building `onnxruntime-gpu` from source
  on aarch64 is documented in the RAGFlow repo.

## Authentication

None. DeepDoc Server is a stateless inference service intended for a
trusted internal network. Do **not** expose port 9390 publicly — restrict
it to the Docker bridge network or a private subnet.
