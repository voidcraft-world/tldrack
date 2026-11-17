import base64
import io
import json
import shlex
from pathlib import Path

import pytest

from tldrack.images import MAX_IMAGE_BODY_BYTES, image_extension, save_image
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


@pytest.fixture
def image_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("tldrack.images.tempfile.tempdir", str(tmp_path))
    return tmp_path


def test_image_bytes_are_preserved_in_a_private_generated_file(image_tmp):
    result = save_image({"data": base64.b64encode(PNG).decode(), "name": "../../evil.sh"})
    path = Path(result["path"])
    assert path.parent == image_tmp
    assert path.suffix == ".png"
    assert path.read_bytes() == PNG
    assert path.stat().st_mode & 0o777 == 0o600
    assert shlex.split(result["paste"]) == [str(path)]
    assert "\n" not in result["paste"] and "\r" not in result["paste"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": 7},
        {"data": "***"},
        {"data": ""},
        {"data": base64.b64encode(b"<svg>script</svg>").decode()},
        {"data": "A" * MAX_IMAGE_BODY_BYTES},
    ],
)
def test_bad_images_never_create_files(image_tmp, payload):
    with pytest.raises(ValueError):
        save_image(payload)
    assert list(image_tmp.iterdir()) == []


@pytest.mark.parametrize(
    "data,extension", [(PNG, ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF89a", ".gif"), (b"RIFF0000WEBP", ".webp")]
)
def test_extension_comes_from_bytes(data, extension):
    assert image_extension(data) == extension


def test_route_guards_size_limit_and_local_storage(image_tmp):
    cls = build_status_handler(
        StatusStore(),
        SessionIconStore(image_tmp / "icons.json"),
        SessionModelStore(image_tmp / "models.json"),
        "voidcraft",
        {"pid": 1},
        ShutdownControl("status-only"),
    )
    raw = json.dumps({"data": base64.b64encode(PNG).decode()}).encode()

    def request(origin="http://localhost:7681", host="localhost:7682", size=None, body=raw):
        handler = object.__new__(cls)
        handler.path = "/images"
        handler.headers = {"Origin": origin, "Host": host, "Content-Length": str(size or len(body))}
        handler.rfile = io.BytesIO(body)
        responses = []
        handler._send_json = lambda status, payload: responses.append((status, payload))
        handler.do_POST()
        return responses[0]

    assert request(origin="https://evil.example")[0] == 403
    assert request(host="rebind.evil.example")[0] == 403
    assert request(size=MAX_IMAGE_BODY_BYTES + 1)[0] == 400
    assert request(body=b"[]")[0] == 400
    assert request(body=b'{"data":"bad"}')[0] == 400
    assert list(image_tmp.glob("tldrack-image-*")) == []
    status, result = request()
    assert status == 201
    assert Path(result["path"]).read_bytes() == PNG
