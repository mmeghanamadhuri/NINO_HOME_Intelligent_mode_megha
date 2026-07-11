# Minimal Server Integration Guide

Use this when you want **emotion recognition inside an existing server** — without copying menus, webcam UIs, Flask demos, or the full expressive pipeline.

You have two options:

| Option | Best for | Model on server? | Internet? |
|--------|----------|------------------|-----------|
| **A — AWS Rekognition** | Cloud deployment, simplest integration | No | Yes |
| **B — Local DrGM model** | Offline / no AWS cost | Yes (~750 MB) | No (after setup) |

Pick **one** (or support both behind a config flag).

---

## Option A — AWS Rekognition

AWS runs face + emotion detection in the cloud. Your server sends image bytes and reads the result.

### Files to copy

```
aws/
  aws_config.py          # loads credentials from .env
  rekognition_client.py  # detect_emotions(image_bytes) → result
  labels.py              # display_name() helper
  .env                   # credentials (never commit to git)
```

**Do not copy:** `main.py`, `webcam.py`, `upload.py`, `face_tracker.py`, `evaluate.py`, `check_env.py`.

### `.env` file

```env
AWS_ACCESS_KEY_ID=your_access_key_id
AWS_SECRET_ACCESS_KEY=your_secret_access_key
AWS_REGION=ap-south-1
```

IAM user needs `rekognition:DetectFaces` (or `AmazonRekognitionReadOnlyAccess`).

### Python dependencies

```txt
boto3>=1.34
python-dotenv>=1.0
opencv-python>=4.8,<5   # only for prepare_jpeg_bytes
numpy
```

### Usage

```python
from rekognition_client import RekognitionEmotionClient, prepare_jpeg_bytes

client = RekognitionEmotionClient()   # load once at startup

jpeg = prepare_jpeg_bytes(bgr_image)
result = client.detect_emotions(jpeg, (width, height))

if result:
    emotion = result.project_emotion    # angry, happy, sad, ...
    confidence = result.confidence      # 0.0 – 1.0
```

### AWS emotion mapping

| AWS label | Server label |
|-----------|--------------|
| HAPPY | happy |
| SAD | sad |
| ANGRY | angry |
| FEAR | fear |
| DISGUSTED | disgust |
| SURPRISED | surprise |
| CALM | neutral |
| CONFUSED | fear |
| UNKNOWN | uncertain |

---

## Option B — Local model + model loader only

**This is just inference on a face image.** No YuNet face detector, no temporal fusion, no valence/arousal primitives, no `expressive_engine.py`.

Your server (or client) is responsible for:
- finding/cropping the face, **or**
- sending an already-cropped face image

### Files to copy

```
emotion/
  emotion_model_config.py       # model paths + settings
  emotion_labels.py             # 7 class names
  emotion_recognition/
    __init__.py
    recognizer.py               # main API: EmotionRecognizer
    model_loader.py             # picks HuggingFace / ONNX / PyTorch backend
    preprocessing.py
    postprocessing.py
    affectnet_labels.py
    backends/
      __init__.py
      huggingface_backend.py    # used by DrGM model
      onnx_backend.py             # imported by model_loader (keep file)
      pytorch_backend.py          # imported by model_loader (keep file)
  models/
    drgm_convnextv2l_fer7/
      config.json
      model.safetensors           # ~750 MB — required
      preprocessor_config.json
```

### Do NOT copy (pipeline / demo code)

| Skip | Why |
|------|-----|
| `expressive_engine.py` | Full pipeline — face track + fusion + primitives |
| `expressive_config.py` | Tuning for that pipeline only |
| `confusion_refinement.py` | Used only by expressive_engine |
| `face_detection_yunet_2023mar.onnx` | Face detection — not part of model loader |
| `webcam_expressive.py` | Demo UI |
| `upload_image_emotion.py` | Flask demo |
| `main.py` | CLI menu |

### Python dependencies

```txt
numpy
opencv-python>=4.8,<5
torch>=2.0
torchvision>=0.15
transformers>=4.40
safetensors
pillow
```

No `onnxruntime` needed unless you switch to the ONNX backend in config.

### Usage

```python
from emotion_recognition.recognizer import EmotionRecognizer

recognizer = EmotionRecognizer()
recognizer.load_model()           # load once at startup (~few seconds)

def predict_emotion(face_bgr: np.ndarray) -> dict:
  """
  face_bgr: OpenCV BGR numpy array of a cropped face.
  Any reasonable size works; model resizes to 224×224 internally.
  """
  result = recognizer.predict(face_bgr)
  return {
      "emotion": result.emotion,              # angry, happy, sad, ...
      "confidence": result.confidence,        # 0.0 – 1.0
      "probabilities": result.probabilities.tolist(),
  }

# on shutdown
recognizer.close()
```

### Batch inference

```python
results = recognizer.predict_batch([face1_bgr, face2_bgr, face3_bgr])
```

### 7 emotion classes

`angry`, `disgust`, `fear`, `happy`, `neutral`, `sad`, `surprise`

### Model details

| Item | Value |
|------|-------|
| Model | DrGM ConvNeXt V2 Large |
| HuggingFace repo | `DrGM/DrGM-ConvNeXt-V2L-Facial-Emotion-Recognition` |
| Local weights | `models/drgm_convnextv2l_fer7/model.safetensors` |
| Input | BGR face crop → resized 224×224 |
| Backend | HuggingFace (default in `emotion_model_config.py`) |

---

## Suggested server layout

```
your_server/
  app/
    api/
      emotion_routes.py
    emotion/
      aws/                        # Option A
        aws_config.py
        rekognition_client.py
        labels.py
      local/                      # Option B — model loader only
        emotion_model_config.py
        emotion_labels.py
        emotion_recognition/
        models/
          drgm_convnextv2l_fer7/
  .env                            # AWS keys (Option A only)
  requirements-emotion.txt
```

---

## Install on server

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-emotion.txt
```

**Option A** `requirements-emotion.txt`:

```txt
boto3>=1.34
python-dotenv>=1.0
opencv-python>=4.8,<5
numpy
```

**Option B** `requirements-emotion.txt`:

```txt
numpy
opencv-python>=4.8,<5
torch>=2.0
torchvision>=0.15
transformers>=4.40
safetensors
pillow
```

---

## Checklist

- [ ] Copied only the files listed above
- [ ] `model.safetensors` present on server (Option B)
- [ ] Model loaded **once** at startup, reused per request
- [ ] `.env` in `.gitignore` (Option A)
- [ ] Face cropping handled by your server or client (Option B)

---

## Summary

| Path | What you copy | Model size | One-liner |
|------|---------------|------------|-----------|
| **AWS** | 3 `.py` + `.env` | 0 | `client.detect_emotions(jpeg, (w, h))` |
| **Local** | `emotion_recognition/` + 2 config files + model folder | ~750 MB | `recognizer.predict(face_bgr)` |

**AWS** → cloud client + env. No weights.

**Local** → model loader package + `drgm_convnextv2l_fer7/` weights. **Nothing else from the demo.**
