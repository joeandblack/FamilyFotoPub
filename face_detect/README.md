# face_detect

FamilyFoto's People package: face detection and recognition providers, the People catalog,
jobs, review APIs and the People web assets. Application code calls
`face_detect.service.create_service(config)` and never imports a model library directly.

Everything about setup, providers, the `people` catalog, moving a library, calibration and the
People pages is in **[docs/PEOPLE.md](../docs/PEOPLE.md)**.

| Folder | Holds |
| --- | --- |
| `providers/` | InsightFace and FaceNet adapters and the process protocol |
| `spaces/` | Space, detector and backend identities, the library check, golden sets |
| `imaging/` | Guarded canonical image decoding |
| `recognition/`, `jobs/` | Matching policy, grouping, Discover people |
| `catalog/`, `storage/` | The `people` schema, its migrations and evidence handling |
| `api/`, `web/` | People HTTP routes and browser assets |
| `cli/` | `setup`, `doctor`, `calibrate`, `conformance`, `catalog` |
| `config/` | Model manifests, `research-models.json`, per-platform dependency locks |
| `testing/`, `tests/` | Synthetic in-process provider and the package tests |

```sh
python -m face_detect.cli.doctor
python -m pytest -q face_detect/tests
```
