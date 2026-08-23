"""Tests for YOLO26 object detection: summaries, throttling, and prompt wiring."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from object_detection_service import (
    EMPTY_SCENE_NOTE,
    ObjectDetectionService,
    phrase_visible_scene,
    spoken_scene_report,
    summarize_detections,
)


class _FakeTensor:
    """Stands in for the torch tensors Ultralytics returns on Boxes."""

    def __init__(self, array: np.ndarray) -> None:
        self._array = array

    def cpu(self) -> "_FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self._array


class _FakeBoxes:
    def __init__(self, xyxy, conf, cls) -> None:
        self.xyxy = _FakeTensor(np.array(xyxy, dtype=np.float32))
        self.conf = _FakeTensor(np.array(conf, dtype=np.float32))
        self.cls = _FakeTensor(np.array(cls, dtype=np.float32))

    def __len__(self) -> int:
        return len(self.conf.numpy())


class _FakeResult:
    def __init__(self, boxes) -> None:
        self.boxes = boxes


class _FakeModel:
    """Records predict() calls so throttling can be asserted."""

    def __init__(self, boxes=None) -> None:
        self.boxes = boxes
        self.calls = 0
        self.names = {0: "person", 1: "laptop"}

    def predict(self, *args, **kwargs):
        self.calls += 1
        return [_FakeResult(self.boxes)]


def _service(model: _FakeModel | None = None, **env: str) -> ObjectDetectionService:
    defaults = {"OBJECT_DETECTION_ENABLED": "1", "OBJECT_DETECTION_INTERVAL_S": "10"}
    defaults.update(env)
    with patch.dict(os.environ, defaults, clear=False):
        service = ObjectDetectionService()
    if model is not None:
        service._model = model
        service._names = dict(model.names)
        service._device = "cpu"
    return service


def _frame() -> np.ndarray:
    return np.zeros((240, 320, 3), dtype=np.uint8)


class SummarizeDetectionsTests(unittest.TestCase):
    def test_empty_detections_produce_no_summary(self) -> None:
        self.assertEqual(summarize_detections([]), "")

    def test_single_object_gets_an_article(self) -> None:
        self.assertEqual(summarize_detections([{"label": "laptop"}]), "a laptop")
        self.assertEqual(summarize_detections([{"label": "apple"}]), "an apple")

    def test_person_pluralizes_irregularly(self) -> None:
        detections = [{"label": "person"}, {"label": "person"}]
        self.assertEqual(summarize_detections(detections), "2 people")

    def test_multiple_classes_are_joined_and_sorted_by_count(self) -> None:
        detections = [
            {"label": "cup"},
            {"label": "person"},
            {"label": "person"},
            {"label": "laptop"},
        ]
        self.assertEqual(summarize_detections(detections), "2 people, a cup and a laptop")


class PhraseVisibleSceneTests(unittest.TestCase):
    def test_empty_scene(self) -> None:
        self.assertEqual(phrase_visible_scene([], []), "")
        self.assertEqual(
            spoken_scene_report([], []), f"{EMPTY_SCENE_NOTE}."
        )

    def test_names_only(self) -> None:
        self.assertEqual(phrase_visible_scene(["Hari"], []), "Hari")
        self.assertEqual(
            phrase_visible_scene(["Hari", "Nora"], []), "Hari and Nora"
        )

    def test_objects_only(self) -> None:
        self.assertEqual(
            phrase_visible_scene([], [{"label": "laptop"}]), "a laptop"
        )

    def test_names_and_objects(self) -> None:
        self.assertEqual(
            phrase_visible_scene(["Hari"], [{"label": "laptop"}]),
            "Hari and a laptop",
        )

    def test_person_class_suppressed_when_names_exist(self) -> None:
        detections = [{"label": "person"}, {"label": "laptop"}]
        self.assertEqual(
            phrase_visible_scene(["Hari"], detections), "Hari and a laptop"
        )
        self.assertIn("person", phrase_visible_scene([], detections))

    def test_spoken_left_right_center(self) -> None:
        self.assertEqual(
            spoken_scene_report(["Hari"], [{"label": "cup"}], pose="center"),
            "Right now I see Hari and a cup.",
        )
        self.assertEqual(
            spoken_scene_report(["Hari"], [], pose="left"),
            "On my left I see Hari.",
        )
        self.assertEqual(
            spoken_scene_report([], [{"label": "chair"}], pose="right"),
            "On my right I see a chair.",
        )
        self.assertEqual(
            spoken_scene_report([], [], pose="left"),
            f"On my left {EMPTY_SCENE_NOTE}.",
        )

    def test_spoken_people_include_emotion(self) -> None:
        self.assertEqual(
            phrase_visible_scene(["Hari"], [], emotions={"Hari": "Happy"}),
            "Hari, who looks happy",
        )
        self.assertEqual(
            spoken_scene_report(
                ["Hari"],
                [{"label": "cup"}],
                pose="center",
                emotions={"Hari": "sad"},
            ),
            "Right now I see Hari, who looks a bit down, and a cup.",
        )
        self.assertEqual(
            spoken_scene_report(
                ["Hari", "Nora"],
                [],
                pose="left",
                emotions={"Hari": "Happy", "Nora": "surprise"},
            ),
            "On my left I see Hari, who looks happy and Nora, who looks surprised.",
        )

    def test_spoken_up_down_at_each_side(self) -> None:
        self.assertEqual(
            spoken_scene_report(
                ["Hari"], [{"label": "cup"}, {"label": "chair"}], pose="right", tilt="up"
            ),
            "Looking up on my right I see Hari and a chair and a cup.",
        )
        self.assertEqual(
            spoken_scene_report([], [{"label": "laptop"}], pose="left", tilt="down"),
            "Looking down on my left I see a laptop.",
        )
        self.assertEqual(
            spoken_scene_report(["Nora"], [], pose="front", tilt="up"),
            "Looking up I see Nora.",
        )
        self.assertEqual(
            spoken_scene_report([], [], pose="right", tilt="down"),
            f"Looking down on my right {EMPTY_SCENE_NOTE}.",
        )


class DetectTests(unittest.TestCase):
    def test_disabled_service_never_runs_inference(self) -> None:
        model = _FakeModel()
        service = _service(model, OBJECT_DETECTION_ENABLED="0")

        self.assertEqual(service.detect(_frame()), [])
        self.assertEqual(model.calls, 0)

    def test_boxes_are_converted_to_xywh_and_clipped_to_frame(self) -> None:
        # Second box runs past the 320x240 frame and must be clamped.
        boxes = _FakeBoxes(
            xyxy=[[10, 20, 110, 140], [300, 200, 400, 300]],
            conf=[0.9, 0.5],
            cls=[0, 1],
        )
        service = _service(_FakeModel(boxes))

        detections = service.detect(_frame())

        self.assertEqual(len(detections), 2)
        self.assertEqual(detections[0]["label"], "person")
        self.assertEqual(detections[0]["box"], {"x": 10, "y": 20, "w": 100, "h": 120})
        clipped = detections[1]["box"]
        self.assertLessEqual(clipped["x"] + clipped["w"], 320)
        self.assertLessEqual(clipped["y"] + clipped["h"], 240)

    def test_detections_are_sorted_by_confidence(self) -> None:
        boxes = _FakeBoxes(
            xyxy=[[0, 0, 10, 10], [20, 20, 40, 40]], conf=[0.4, 0.95], cls=[0, 1]
        )
        service = _service(_FakeModel(boxes))

        detections = service.detect(_frame())

        self.assertEqual([d["label"] for d in detections], ["laptop", "person"])

    def test_max_objects_caps_results(self) -> None:
        boxes = _FakeBoxes(
            xyxy=[[0, 0, 10, 10]] * 5, conf=[0.9] * 5, cls=[0] * 5
        )
        service = _service(_FakeModel(boxes), OBJECT_DETECTION_MAX_OBJECTS="2")

        self.assertEqual(len(service.detect(_frame())), 2)

    def test_repeat_calls_inside_interval_reuse_cached_detections(self) -> None:
        boxes = _FakeBoxes(xyxy=[[0, 0, 10, 10]], conf=[0.9], cls=[0])
        model = _FakeModel(boxes)
        service = _service(model)

        first = service.detect(_frame())
        second = service.detect(_frame())

        self.assertEqual(model.calls, 1)
        self.assertEqual(first, second)

    def test_force_bypasses_cached_detections(self) -> None:
        boxes = _FakeBoxes(xyxy=[[0, 0, 10, 10]], conf=[0.9], cls=[0])
        model = _FakeModel(boxes)
        service = _service(model)

        service.detect(_frame())
        service.detect(_frame(), force=True)

        self.assertEqual(model.calls, 2)

    def test_cache_is_kept_per_device(self) -> None:
        boxes = _FakeBoxes(xyxy=[[0, 0, 10, 10]], conf=[0.9], cls=[0])
        model = _FakeModel(boxes)
        service = _service(model)

        service.detect(_frame(), device_id="robot-a")
        service.detect(_frame(), device_id="robot-b")

        self.assertEqual(model.calls, 2)
        self.assertEqual(len(service.latest("robot-a")), 1)
        self.assertEqual(service.latest("unseen-device"), [])

    def test_inference_failure_is_swallowed(self) -> None:
        model = _FakeModel()
        model.predict = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cuda oom"))
        service = _service(model)

        self.assertEqual(service.detect(_frame()), [])
        self.assertIn("cuda oom", service.stats()["last_error"])

    def test_missing_model_does_not_retry_every_frame(self) -> None:
        service = _service()
        with patch.object(
            ObjectDetectionService, "_load_model", side_effect=RuntimeError("no weights")
        ) as load:
            self.assertEqual(service.detect(_frame()), [])
            self.assertEqual(service.detect(_frame()), [])
        self.assertEqual(load.call_count, 1)


class AnnotateTests(unittest.TestCase):
    def test_annotate_draws_on_the_frame(self) -> None:
        service = _service()
        frame = _frame()
        detections = [
            {"label": "person", "class_id": 0, "confidence": 0.9,
             "box": {"x": 10, "y": 10, "w": 50, "h": 60}}
        ]

        service.annotate(frame, detections)

        self.assertTrue(frame.any())

    def test_annotate_with_no_detections_leaves_frame_untouched(self) -> None:
        service = _service()
        frame = _frame()

        service.annotate(frame, [])

        self.assertFalse(frame.any())


class WeightsDirTests(unittest.TestCase):
    """Ultralytics strips apostrophes from weights paths, so we must not use one."""

    def test_project_models_dir_is_preferred(self) -> None:
        import object_detection_service as ods

        with patch.object(ods, "MODEL_DIR", Path("/opt/nino/server/data/models")):
            self.assertEqual(ods._weights_dir(), Path("/opt/nino/server/data/models"))

    def test_apostrophe_in_project_path_falls_back_outside_the_project(self) -> None:
        import object_detection_service as ods

        with patch.object(ods, "MODEL_DIR", Path("/home/me/P4 server Don't Touch/models")):
            resolved = ods._weights_dir()

        self.assertNotIn("'", str(resolved))
        self.assertEqual(resolved, Path.home() / ".cache" / "nino" / "models")


class VoicePromptTests(unittest.TestCase):
    @patch("llm_service.ollama_generate")
    def test_scene_is_injected_into_the_voice_prompt(self, generate) -> None:
        from llm_service import answer_voice_query

        generate.return_value = "I can see a laptop on the desk."

        answer_voice_query("what do you see?", vision_context="a laptop and a cup")

        prompt = generate.call_args[0][0]
        self.assertIn("a laptop and a cup", prompt)

    @patch("llm_service.ollama_generate")
    def test_prompt_has_no_vision_block_without_detections(self, generate) -> None:
        from llm_service import answer_voice_query

        generate.return_value = "Sure thing."

        answer_voice_query("what time is it?", vision_context="")

        prompt = generate.call_args[0][0]
        self.assertNotIn("Your camera can see right now", prompt)


if __name__ == "__main__":
    unittest.main()
