"""Wektory (osadzenia) opisów „zdjęcie telefonu zamiast telefonu” dla analizy zdjęć CLIP.

Uruchamiane w GitHub Actions (model tekstowy CLIP + torch nie wchodzą do programu). Wypisuje wiersze
base64 (fp16) z sumami kontrolnymi do wklejenia w ``phonebot/ml/photo_model.py`` (``SCAM_CLASSES``).
Sprawdza też spójność: wektor „smartphone” policzony tu = wektor zapisany w programie (ta sama metoda).
"""
from __future__ import annotations

import base64
import hashlib
import sys

import numpy as np
import torch
from transformers import CLIPModel, CLIPTokenizer

sys.path.insert(0, "scripts")
from clip_prepare import PROMPT_SETS  # noqa: E402

SCAM_PROMPTS = {
    "printed_photo": [
        "a printed photo of a phone on paper", "a photograph of an iPhone printed on a sheet of paper",
        "a paper printout of a smartphone picture", "a printed picture of an iPhone lying on a table",
        "a photo print of a mobile phone", "a glossy photo print showing an iPhone",
    ],
    "poster": [
        "a poster with a smartphone", "a framed poster of an iPhone", "a wall poster showing a mobile phone",
        "an art print of an iPhone hanging on a wall", "a picture frame with a photo of a phone",
    ],
    "listing_screenshot": [
        "a screenshot of an online listing", "a screenshot of a phone advertisement on a website",
        "a screenshot of an online shop page with a phone and a price", "a screenshot of a marketplace listing",
        "a screenshot of a web page showing an iPhone for sale",
    ],
}


def main() -> int:
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").eval()
    tok = CLIPTokenizer.from_pretrained("openai/clip-vit-base-patch32")

    def embed(prompts: list[str]) -> np.ndarray:
        with torch.no_grad():
            t = tok(prompts, padding=True, return_tensors="pt")
            pooled = model.text_model(input_ids=t["input_ids"], attention_mask=t["attention_mask"]).pooler_output
            f = model.text_projection(pooled)
            f = f / f.norm(dim=-1, keepdim=True)
            m = f.mean(0)
            return (m / m.norm()).numpy().astype(np.float32)

    phone = embed(PROMPT_SETS["C"]["smartphone"]).astype(np.float16)
    print("CHECK smartphone", hashlib.sha256(phone.tobytes()).hexdigest()[:16], flush=True)
    rows = np.stack([embed(p) for p in SCAM_PROMPTS.values()]).astype(np.float16)
    print("SCAM_CLASSES", list(SCAM_PROMPTS), flush=True)
    print("SCAM_SHA256", hashlib.sha256(rows.tobytes()).hexdigest(), flush=True)
    print("SCAM_B64", base64.b64encode(rows.tobytes()).decode(), flush=True)
    # podobieństwa między klasami (czy opisy się nie pokrywają)
    allv = np.vstack([phone.astype(np.float32), rows.astype(np.float32)])
    allv /= np.linalg.norm(allv, axis=1, keepdims=True)
    print("SIMILARITY (smartphone, printed_photo, poster, listing_screenshot)", flush=True)
    print(np.round(allv @ allv.T, 3), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
