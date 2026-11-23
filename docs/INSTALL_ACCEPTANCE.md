# FamilyFoto install acceptance note

Read this before installing. Installing FamilyFoto downloads and installs software written
by other people, each under its own terms, and the optional People feature downloads face
models whose terms **limit them to non-commercial research**. FamilyFoto cannot decide for
you whether your use is allowed. This note lists what is installed and what you are accepting.
It is not legal advice.

Facts below were taken from the packages the macOS (Apple Silicon) installer actually
installed on 2026-11-05, and from Homebrew. Versions are the pinned ones in
`requirements.txt` and `face_detect/config/dependencies/`. Windows uses its own pinned lists
(`requirements-faces-windows.txt`, `face_detect/config/dependencies/*-windows-amd64.txt`); check
those before a Windows install.

## What you are agreeing to

By installing, you confirm that you have read this note and that:

1. You accept the licence of each third-party component below, including the copyleft ones
   (FFmpeg as built by Homebrew is GPL-3.0-or-later; `psycopg` is LGPL-3.0).
2. **If you install People**, you accept the terms of the face models:
   - InsightFace `buffalo_l`: the package states that the pretrained models are for
     **non-commercial research only**, whether downloaded automatically or manually. Its code
     is MIT. Model terms apply separately from the library's MIT licence.
   - You will use the models only in a way those terms allow. The installer cannot judge
     whether private family use of your photos counts. If you are unsure, do not install
     People, or ask the model's owners.
3. The installer downloads and installs software from the internet (Homebrew formulae, Python
   packages from PyPI, and the model archive from GitHub). Setup is an explicit network
   operation. Inference never downloads anything, and recognition runs in local Python
   processes. Photos are not sent anywhere by the installer.
4. You are installing for yourself, on a computer you control, and you accept that this is
   pre-release software.

**How agreement is given.** People is installed only with the explicit `--research-only` flag
(`install --with-people --research-only`, or `installer/macos/people.py install --research-only`).
The installer refuses People without it. Passing the flag means you have read and accepted the
model terms in this note. An install without People needs no model agreement.

**How to decline.** Install Core only (no `--with-people`). The app then has no People tab and
no Discover people stage. People can be added later with the standalone step in
[installer/macos/README.md](../installer/macos/README.md).

## What the macOS installer installs

### Tools from Homebrew (must already be present, or are installed by setup)

| Component | Version | Licence (as listed by Homebrew) |
| --- | --- | --- |
| Python | 3.12.16 | Python-2.0 |
| PostgreSQL | 17.11 | PostgreSQL |
| FFmpeg | 9.0.2 | GPL-3.0-or-later |

Homebrew itself is never installed silently. Installing a missing formula may update its
shared dependencies.

### Core application libraries (private `.venv`)

| Package | Version | Licence reported by the package |
| --- | --- | --- |
| fastapi | 0.115.12 | MIT |
| uvicorn | 0.34.2 | BSD-3-Clause |
| psycopg, psycopg-binary | 3.2.9 | LGPL-3.0 |
| psycopg-pool | 3.3.3 | LGPL-3.0-only |
| Pillow | 11.2.1 | MIT-CMU |
| pillow-heif | 1.8.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| cryptography | 44.0.3 | Apache-2.0 OR BSD-3-Clause |
| numpy | 1.26.4 | BSD-style (NumPy Developers) |
| scipy | 1.16.3 | BSD |
| starlette, h11, httpcore, click, idna, certifi (MPL-2.0), anyio, pydantic and their dependencies | pinned | MIT, BSD-3-Clause, MPL-2.0 or Apache-2.0 as reported by each package |

The full list, with versions, is whatever `pip list` shows in `.venv`.

### People (optional, only with `--research-only`)

A separate Python environment at `runtime/face_detect/envs/insightface`, plus the model.

| Component | Version | Licence reported by the package |
| --- | --- | --- |
| insightface | 2.1 | Code: MIT (stated in its description; the package metadata field is empty) |
| onnxruntime | 1.31.1 | MIT |
| onnx | 1.23.2 | Apache-2.0 |
| opencv-python | 5.0.0.93 | Apache 2.0 |
| numpy | 2.5.4 | BSD-3-Clause and other permissive licences |
| scipy | 1.18.1 | BSD |
| scikit-image | 0.26.0 | BSD-style; the package metadata says only "Files: *", so read its LICENSE |
| requests | 2.34.2 | Apache-2.0 |
| protobuf | 7.36.2 | BSD-3-Clause |
| flatbuffers | 25.12.19 | Apache 2.0 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| ImageIO, tifffile, networkx, lazy-loader, ml_dtypes, packaging, Pillow (MIT-CMU), and others | pinned | permissive, as reported by each package |

| Model | Source | Terms |
| --- | --- | --- |
| InsightFace `buffalo_l` (`det_10g.onnx`, `w600k_r50.onnx`) | `https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_l.zip` | **Non-commercial research only** (see above). Archive SHA-256 `80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f`; each file's SHA-256 is pinned in `face_detect/config/research-models.json` and checked after download |

Matching starts review-only. Autostart is off. Nothing is scanned until you start it.

### Not installed by default

FaceNet (`facenet-pytorch` with InceptionResnetV1 trained on VGGFace2, and PyTorch) is an
optional second provider set up with `python -m face_detect.cli.setup --provider facenet --research-only`.
Review its upstream terms
([facenet-pytorch](https://github.com/timesler/facenet-pytorch)) and those of the VGGFace2
training data before using it. It is not part of the default install.

## Not covered

This note covers what the installer installs. It does not cover FamilyFoto's own licence, the
terms of your photos, or terms that change after the date above. Component licences can change
with a new version: re-check when the pinned versions change.
