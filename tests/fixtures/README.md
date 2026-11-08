# Recognition test fixtures

Add these local images to run the `analyse_frame` fixture tests:

| File | Contents |
| --- | --- |
| `one_face.jpg` | Exactly one clear, front-facing face. |
| `no_face.jpg` | A scene with no people. |
| `two_faces.jpg` | Two or more clearly visible faces. |

The tests skip these cases until the files exist. Do not commit images of people without their
consent; keep personal fixtures untracked if necessary.
