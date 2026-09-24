"""Deploy one isolated image model: ALADIN_EXTRA_IMAGE_MODEL=<id> modal deploy ..."""
import os
from pathlib import Path
import modal
from aladin.image_models import MODELS, DEFAULT_MODEL
from aladin_modal_app import _image

MODEL = os.environ.get('ALADIN_EXTRA_IMAGE_MODEL', 'anima-base-1.0')
if MODEL == DEFAULT_MODEL or MODEL not in MODELS:
    raise ValueError('Select Anima or Pony')
SPEC = MODELS[MODEL]
ROOT = Path(__file__).parent
# Reuse the verified Comfy dependency layers; each app gets its own model Volume.
image = _image().env({'ALADIN_EXTRA_IMAGE_MODEL': MODEL}).add_local_file(
    ROOT / 'aladin_extra_image_modal_app.py', '/opt/aladin_extra_image_modal_app.py', copy=True)
for name in ('image_models.py', 'extra_image_request.py', 'extra_image_worker.py'):
    image = image.add_local_file(ROOT / 'aladin' / name, '/opt/aladin/' + name, copy=True)
app = modal.App(SPEC['app'], include_source=False)
cache = modal.Volume.from_name(SPEC['app'].replace('-image-', '-models-'), create_if_missing=True)
results = modal.Volume.from_name('aladin-image-results-v1', create_if_missing=True)

@app.function(image=image, timeout=1800, volumes={'/models': cache})
def prepare_models():
    from aladin.extra_image_worker import ensure_weights
    result = ensure_weights(MODEL, Path('/models'))
    cache.commit()
    return result

@app.function(image=image, gpu='L40S', cpu=4, memory=32768, timeout=3600,
              startup_timeout=1200, retries=0, min_containers=0, max_containers=1,
              scaledown_window=2, volumes={'/models': cache, '/results': results})
def generate(request: dict, source: bytes = b'') -> dict:
    from aladin.extra_image_worker import execute
    if request.get('modelId') != MODEL or source:
        raise ValueError('Wrong model or unsupported source image')
    record = execute(request, '/results')
    cache.commit()
    results.commit()
    return {k: record[k] for k in ('images', 'elapsedSeconds')}
