# 10Eros-Max video rollout — 2026-09-23

Status: deployed to Modal and workstation; default GPU generation, production API, artifact download, and authenticated public browser playback verified.

The user explicitly accepted the author's "NSFW capable" wording for this named model.
No content filtering or prompt rewriting was added. Validation uses an ordinary blue teapot image.

## Pinned weights and workflow

- Model: [TenStrip/10Eros-Max](https://huggingface.co/TenStrip/10Eros-Max), revision `8a198588c8870ab0d613b3492a3150d091c8c2dd`.
- Checkpoint: `10Eros_Max_h3_TURBO-hybrid_beta5_int8.safetensors` (20,970,414,464 bytes). Author marks beta3/4 corrupted and beta5 functional.
- Support: [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3), revision `0fea91688aefb62d4eb94d5952f277f46c298284`; Qwen3-VL 32B NVFP4 AWQ encoder, FP16 video VAE, FP32 audio VAE. Total weights ~42.47 GB.
- ComfyUI: `c194dd00cd42aa18d9dbf27d977bf6b85d9ea565`, native MiniMax-H3 nodes.
- Defaults: 6 steps, CFG 1, res_multistep/simple, video sigma shift 12, audio shift 3, 832×480, 124 frames at 24fps (~5.2s).
- Presets: short 56 frames (~2.3s), normal 124 (~5.2s), long 192 (8s). Model frame grid is 17n+5. HD presets explicitly remain untested.
- Checkpoint has baked Turbo acceleration. Legacy `lora_strength` stays accepted only at neutral 1; the old Wan LoRA is never loaded.
- Input image conditions H3 jointly generated video/audio; output stays WebM (VP9 + Opus) for existing API/storage/browser compatibility. CFG ≠1 uses image-conditioned positive and negative branches; default CFG1 does not use negative conditioning.

## Resources and verification

- New Modal app `aladin-video-h3-v1`, model cache `aladin-h3-models-v1`; result volume `aladin-video-results-v1` retained.
- L40S, 96 GiB RAM, max one GPU container, min zero. CPU-only predownload avoids GPU billing while fetching weights.
- Cache keys include model, pinned model revision and worker source hash, preventing a previous Wan receipt from satisfying an H3 request.
- Native node probe found every required node; SaveVideo uses the dynamic nested API input `format.codec`.
- Unit/integration tests: 98 passed; tests cover public presets, obsolete frame rejection, baked Turbo parameter rejection, cache separation, joint audio/video graph and CFG branch.
- Local evidence lives in ignored `.cache/h3-audit/` (node schemas, deploy logs, requests, receipts, generated artifact).

## Actual execution and production verification

- Modal call: `fc-01M36HFDQ2AMVAF7RPQN886QFD` on L40S. Blue teapot, default 832×480 / 124 frames / 6 steps / CFG1, seed 20260923.
- Container elapsed 138.66 seconds, including model load and WebM encoding, excluding provider cold-start queue (~2m43s). ComfyUI execution 119.60 seconds; six sampling steps ~68 seconds.
- Receipt reports `TenStrip/10Eros-Max` and the exact beta5 INT8 checkpoint. Worker SHA256 `e1638e4fdd85a8a94e077816d47a42140083bbdd7b7b9897b7c613ad3c28bacb` matches workstation runtime and local source.
- Output: 338,329 bytes, SHA256 `d1c815d1f414d3cb1d57b26f8e4fadeda1ed42667a58dabbac46d91c1355f78d`; full ffmpeg decode succeeds.
- ffprobe: 124 VP9 frames, 832×480, 24fps; stereo Opus 48kHz; mux duration 5.192s. First/middle/last frames visually show the teapot, camera push and changing window light. Audio stream verified technically; perceptual audio quality not reviewed.
- Production job: [`6a49c13e1ab44b2e9ef916f8d0e99ae3`](https://aladin.example.com/jobs/6a49c13e1ab44b2e9ef916f8d0e99ae3), succeeded. Same request reuses the tested Modal receipt; upload/enqueue/worker retrieval/local persistence/download succeeded without another sampling run.
- API artifact bytes match the original Modal SHA256. This check found a pre-existing `image/png` response type on the job-artifact API; fixed to `video/webm`, with shared UI/API download regression coverage (14 video route tests passed).
- Public authenticated Chrome: task completed, player readyState 4, 832×480, duration 5.192, playing without an error. Video creation form shows beta5 Turbo, correct duration presets, res_multistep/simple defaults.
- Existing image generation, stored artifacts, Cloudflare route and Access policy unchanged. Short/long/HD and non-default CFG are parameter-validated but not GPU-tested in this rollout.
