"""Przygotowanie i sprawdzenie modelu CLIP do analizy zdjęć (uruchamiane w GitHub Actions).

1. Pobiera eksporty ONNX modelu openai/clip-vit-base-patch32 (repozytorium Xenova na Hugging Face)
   i wypisuje ich rozmiary, sumy SHA-256 oraz wejścia/wyjścia.
2. Liczy osadzenia (embeddingi) opisów klas — smartfon / etui / szkło / pudełko — oryginalnym modelem
   (transformers + torch) i wypisuje je, żeby aplikacja nie potrzebowała modelu tekstowego ani torcha.
3. Sprawdza skuteczność na prawdziwych zdjęciach z Vinted (etykiety z tytułów), dla każdego wariantu ONNX.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict

import httpx
import numpy as np
import onnxruntime as ort
import torch
from PIL import Image
from transformers import CLIPModel, CLIPTokenizer

HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/"
VARIANTS = ["vision_model_quantized.onnx", "vision_model_fp16.onnx", "vision_model.onnx"]
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

_BASE = {
    "smartphone": [
        "a photo of a smartphone", "a photo of an iPhone", "a photo of a used mobile phone",
        "a photo of the back of an iPhone", "a photo of a phone with a cracked screen",
        "a photo of a person holding a smartphone", "a photo of a smartphone in a protective case",
        "a photo of a smartphone lying next to its box", "a photo of a phone screen showing the home screen",
    ],
    "case": [
        "a photo of an empty phone case", "a photo of a phone case without a phone", "a photo of a silicone phone cover",
        "a photo of a clear plastic phone case", "a photo of a wallet phone case", "a photo of several phone cases",
    ],
    "screen_protector": [
        "a photo of a screen protector", "a photo of tempered glass screen protectors",
        "a photo of a screen protector in its package", "a photo of a thin sheet of glass for a phone screen",
    ],
    "box": [
        "a photo of an empty phone box", "a photo of an empty iPhone box", "a photo of a white cardboard product box",
        "a photo of the packaging of a phone",
    ],
}
_PHONE_B = [
    "a photo of an iPhone", "a product photo of an Apple iPhone", "a photo of the back of an iPhone with its camera",
    "a photo of an iPhone screen turned on", "a photo of a used smartphone on a table",
    "a photo of a smartphone with a cracked screen", "a photo of a person holding an iPhone",
    "a photo of a smartphone in a case", "a photo of a smartphone next to its box",
    "a screenshot of an iPhone settings screen",
]
_CASE_B = [
    "a photo of an empty phone case", "a photo of a phone case with nothing inside", "a photo of phone cases for sale",
    "a photo of a silicone phone case", "a photo of a transparent phone case", "a photo of a wallet phone case",
    "a photo of a decorative phone case with a pattern",
]
_PROT_B = ["a photo of a screen protector", "a photo of tempered glass screen protectors",
           "a photo of a screen protector package", "a photo of a glass screen protector kit"]
_BOX_B = ["a photo of an empty iPhone box", "a photo of an empty phone box", "a photo of a white phone box",
          "a photo of phone packaging without a phone"]
PROMPT_SETS = {
    "A": _BASE,
    "B": {"smartphone": _PHONE_B, "case": _CASE_B, "screen_protector": _PROT_B, "box": _BOX_B},
    "C": {"smartphone": _PHONE_B + ["a screenshot of the iPhone battery health screen",
                                    "a photo of an iPhone with a cracked back glass",
                                    "a photo of an iPhone lying face down"],
          "case": _CASE_B, "screen_protector": _PROT_B, "box": _BOX_B},
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def say(*a):
    print(*a, flush=True)


def preprocess(data: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    s = 224 / min(w, h)
    img = img.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - MEAN) / STD
    return arr.transpose(2, 0, 1)[None]


def main() -> int:
    client = httpx.Client(follow_redirects=True, timeout=120, headers={"User-Agent": UA})
    sessions = {}
    for name in VARIANTS:
        t = time.time()
        r = client.get(HF + name)
        say(f"MODEL {name}: HTTP {r.status_code}, {len(r.content) / 1e6:.1f} MB, {time.time() - t:.1f} s, "
            f"sha256={hashlib.sha256(r.content).hexdigest()}")
        if r.status_code != 200:
            continue
        sess = ort.InferenceSession(r.content, providers=["CPUExecutionProvider"])
        say("  inputs:", [(i.name, i.shape, i.type) for i in sess.get_inputs()])
        say("  outputs:", [(o.name, o.shape, o.type) for o in sess.get_outputs()])
        sessions[name] = sess

    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").eval()
    tok = CLIPTokenizer.from_pretrained("openai/clip-vit-base-patch32")
    classes = list(_BASE)
    say("CLASSES", json.dumps(classes))

    def embed(prompts: dict[str, list[str]]) -> np.ndarray:
        out = []
        with torch.no_grad():
            for c in classes:
                t = tok(prompts[c], padding=True, return_tensors="pt")
            # jawnie z warstw modelu — API get_text_features zmienia się między wersjami transformers
                pooled = model.text_model(input_ids=t["input_ids"], attention_mask=t["attention_mask"]).pooler_output
                f = model.text_projection(pooled)
                f = f / f.norm(dim=-1, keepdim=True)
                m = f.mean(0)
                out.append((m / m.norm()).numpy())
        return np.stack(out).astype(np.float32)

    embs = {name: embed(prompts) for name, prompts in PROMPT_SETS.items()}
    scale = float(model.logit_scale.exp())
    say("LOGIT_SCALE", scale)
    for name, emb in embs.items():
        say(f"EMB_B64 {name}", base64.b64encode(emb.astype(np.float16).tobytes()).decode())

    def torch_image(pix: np.ndarray) -> np.ndarray:
        with torch.no_grad():
            pooled = model.vision_model(pixel_values=torch.from_numpy(pix)).pooler_output
            f = model.visual_projection(pooled)
        f = f / f.norm(dim=-1, keepdim=True)
        return f.numpy()[0]

    def onnx_image(sess, pix: np.ndarray) -> np.ndarray:
        name = sess.get_inputs()[0].name
        dtype = np.float16 if "float16" in sess.get_inputs()[0].type else np.float32
        outs = sess.run(None, {name: pix.astype(dtype)})
        names = [o.name for o in sess.get_outputs()]
        v = outs[names.index("image_embeds")] if "image_embeds" in names else outs[0]
        v = np.asarray(v, dtype=np.float32).reshape(-1)[:512]
        return v / np.linalg.norm(v)

    def probs(v: np.ndarray, emb: np.ndarray) -> np.ndarray:
        logits = scale * emb @ v
        e = np.exp(logits - logits.max())
        return e / e.sum()

    # --- prawdziwe zdjęcia z Vinted (etykiety z tytułów) ---
    client.head("https://www.vinted.pl/catalog")
    token = client.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Referer": "https://www.vinted.pl/catalog"}
    phone_hint = re.compile(r"\b\d{2,4}\s?gb\b|unlocked|\blocked\b|at&t|verizon|t-mobile|cricket|includ", re.I)
    rules = [
        ("screen_protector", re.compile(r"screen protector|tempered glass|glass protector|protection écran", re.I)),
        ("box", re.compile(r"\bempty\b.*\bbox\b|\bbox only\b|\bonly box\b|boîte vide|original box only", re.I)),
        ("case", re.compile(r"\bcases?\b|\bcover\b|coque|hülle|etui|obal|funda|custodia", re.I)),
    ]
    samples: list[tuple[str, str, str]] = []
    seen = set()
    queries = [("iphone 13 pro", 150), ("iphone 14", 150), ("iphone 12 unlocked", 100), ("iphone case", 0),
               ("iphone screen protector", 0), ("iphone empty box", 0), ("iphone 15 pro max", 200),
               ("iphone 11 64gb", 100), ("iphone box only", 0)]
    for q, pmin in queries:
        params = {"search_text": q, "per_page": 40, "order": "relevance"}
        if pmin:
            params["price_from"] = pmin
        r = client.get("https://api.vinted.pl/svc-catalogue/items", params=params, headers=h)
        time.sleep(2)
        for it in r.json().get("items", []) if r.status_code == 200 else []:
            title = it.get("title", "")
            if it["id"] in seen:
                continue
            price = float((it.get("price") or {}).get("amount") or 0)
            label = None if phone_hint.search(title) else next((lab for lab, rx in rules if rx.search(title)), None)
            if label is None and re.search(r"iphone\s?\d{1,2}", title, re.I) and price >= 100 and \
                    not any(rx.search(title) for _, rx in rules):
                label = "smartphone"
            if label is None:
                continue
            thumbs = (it.get("photo") or {}).get("thumbnails") or []
            url = next((t["url"] for t in thumbs if t.get("type") == "thumb310x430"), None)
            if url:
                seen.add(it["id"])
                samples.append((label, title, url))
    per_class = defaultdict(list)
    for s in samples:
        per_class[s[0]].append(s)
    picked = [s for c in per_class for s in per_class[c][:20]]
    say("SAMPLES", dict(Counter(s[0] for s in picked)))

    rows = []  # (prawda, tytuł, wektor fp16, wektor torch)
    fp16 = sessions.get("vision_model_fp16.onnx")
    for label, title, url in picked:
        try:
            data = client.get(url).content
            pix = preprocess(data)
        except Exception as e:  # noqa: BLE001
            say("  skip", title, e)
            continue
        time.sleep(0.5)
        ref = torch_image(pix)
        v16 = onnx_image(fp16, pix) if fp16 else ref
        rows.append((label, title, v16, ref))
    say(f"COSINE fp16 vs torch: min {min(float(r[2] @ r[3]) for r in rows):.4f}")
    for name, emb in embs.items():
        preds = [(lab, title, probs(v, emb)) for lab, title, v, _ in rows]
        correct = sum(1 for lab, _, p in preds if classes[int(p.argmax())] == lab)
        say(f"SET {name}: ACCURACY {correct}/{len(preds)} = {correct / max(1, len(preds)):.0%}")
        for c in classes:
            sub = [p for lab, _, p in preds if lab == c]
            if sub:
                ok = sum(1 for p in sub if classes[int(p.argmax())] == c)
                say(f"   {c:16} {ok}/{len(sub)}")
        phones = [p for lab, _, p in preds if lab == "smartphone"]
        acc = [p for lab, _, p in preds if lab != "smartphone"]
        for thr in (0.6, 0.7, 0.8, 0.9):
            fc = sum(1 for p in phones if p[1:].max() >= thr)
            caught = sum(1 for p in acc if p[1:].max() >= thr)
            say(f"   próg {thr}: telefony z fałszywym „akcesorium” {fc}/{len(phones)}, akcesoria wykryte {caught}/{len(acc)}")
        agree = sum(1 for p in phones if p[0] >= 0.5)
        say(f"   telefony z „smartfon” ≥ 0.5: {agree}/{len(phones)}")
        if name == "C":
            for lab, title, p in preds:
                say(f"   [{lab:16}] {title[:48]:48} -> " + " ".join(f"{c}:{x:.2f}" for c, x in zip(classes, p, strict=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
