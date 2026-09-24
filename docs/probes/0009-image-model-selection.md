# 0009 — Anima Base 1.0 and Pony Realism 2.2

2026-09-23. Existing Qwen model retained. Scope: text-to-image only; director,
img2img and instruction-edit keep their existing models.

## Pinned weights

- Anima: `circlestone-labs/Anima`, revision `f973fc41ec7545364ac9776c2440285f43ff2a30`.
  `anima-base-v1.0.safetensors`, Qwen3 0.6B encoder, Qwen-image VAE.
  Base 1.0, not Preview, Aesthetic or Turbo. CircleStone Non-Commercial License.
- Pony: author ZyloO, Civitai model 372465 / version 914390, Main + VAE.
  HF mirror `Ine007/ponyRealism_v22MainVAE`, revision `8920738fb34a8b286d80a8af65e37fc41786d88a`.
  Checkpoint SHA256 `7c97ecf786a50a54835a22277c35703787b840e98c04c318a4e3fef9d3b463f7`,
  cross-checked against the independent ArtChicken mirror. Direct Civitai API was unavailable;
  the version/card mapping is preserved by the linked HF card, not independently read from Civitai.
- All downloaded files matched exact byte sizes and SHA256 in `aladin/image_models.py`.

Sources:
- https://huggingface.co/circlestone-labs/Anima
- https://huggingface.co/TheImposterImposters/PonyRealism-v2.2MainVAE/blob/main/README.md
- https://huggingface.co/Ine007/ponyRealism_v22MainVAE
- https://civitai.com/models/372465?modelVersionId=914390

## Runtime and interface

- Separate Modal apps: `aladin-anima-image-v1`, `aladin-pony-image-v1`.
  Each uses its own model cache, L40S / 4 CPU / 32 GiB, min_containers=0.
- Model loading and inference use pinned native ComfyUI nodes. Anima's native
  sampling configuration supplies shift=3; Pony uses baked VAE and Clip Skip=2.
- Defaults: Anima 35 steps / CFG 4.5 / er_sde + simple;
  Pony 30 / CFG 6.5 / dpmpp_2m_sde + karras.
- Square 1024x1024; portrait 832x1216; landscape 1216x832. Qwen sizes unchanged.
- UI and API share parameter normalization. Missing fields use selected-model defaults;
  explicit empty negative stays empty. Positive prompts are never silently rewritten.
- `/api/v1/params.image_models` and skill CLI expose the contracts.
- Model ID and exact parameters survive receipts, artifacts, tuning/reuse and seed variation.
  Existing Qwen idempotency keys are unchanged.

## Verification

- 115 unit/integration tests passed, including HTTP defaults, duplicate submission,
  empty negative form, model routing, version rejection and reuse.
- Workstation API and worker deployed. Postgres retained; no active jobs before deployment.
- Public browser: model selector and all model-specific defaults verified;
  Pony submitted through https://aladin.example.com, without a Tailscale navigation.
- Anima successful production job: `a3ae86ec644246fdafb2f7441aa290f8`.
- Pony successful production job: `43e025b5165a4f1486993628b8165823`.
- Both jobs generated benign teapot scenes at defaults and seed 9232026.
  Downloaded PNGs are 1024x1024 and match receipt SHA256; visually inspected.
  Anima produced a red teapot illustration (imperfect spout geometry); Pony produced
  a photographic tea set. This is transport/model smoke proof, not a quality benchmark.
- Live Pony reuse page preserves model, seed, sampler and dimensions; switching to
  Anima replaces defaults and choices while retaining the prompt and seed.
