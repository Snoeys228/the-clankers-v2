"""
Load files -> split into chunks -> embed with EmbeddingGemma -> similarity checks.

Install:
    pip install -U sentence-transformers pypdf python-docx

Note: google/embeddinggemma-300m is a gated model. Accept the license on
Hugging Face and run `huggingface-cli login` once.

Usage examples:
    # 1) Semantic search of a query across all chunks
    python chunk_similarity.py docs/ --query "What is the refund policy?" --top-k 5

    # 2) Find similar / duplicated chunks (across different files)
    python chunk_similarity.py docs/ --pairs --threshold 0.8

    # 3) Both, with custom chunking
    python chunk_similarity.py a.txt b.pdf --chunk-words 150 --overlap 30 --pairs --query "hello"
"""

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer

SUPPORTED = {".txt", ".md", ".pdf", ".docx"}


# ----------------------------------------------------------------------------
# 1. Loading files
# ----------------------------------------------------------------------------
def read_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    raise ValueError(f"Unsupported file type: {ext}")


def collect_files(inputs: list[str]) -> list[Path]:
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files += [f for f in sorted(p.rglob("*")) if f.suffix.lower() in SUPPORTED]
        elif p.is_file():
            files.append(p)
        else:
            print(f"Skipping (not found): {item}")
    return files


# ----------------------------------------------------------------------------
# 2. Chunking (sentence-aware, word-based size, with overlap)
# ----------------------------------------------------------------------------
@dataclass
class Chunk:
    source: str
    index: int
    text: str


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def chunk_text(text: str, chunk_words: int = 200, overlap: int = 40) -> list[str]:
    """Group sentences into chunks of ~chunk_words words; keep `overlap` words
    from the end of the previous chunk so context isn't cut off abruptly."""
    chunks, current, count = [], [], 0
    for sent in split_sentences(text):
        n = len(sent.split())
        if current and count + n > chunk_words:
            chunks.append(" ".join(current))
            # build overlap from the tail of the previous chunk
            tail, tail_count = [], 0
            for s in reversed(current):
                w = len(s.split())
                if tail_count + w > overlap:
                    break
                tail.insert(0, s)
                tail_count += w
            current, count = tail, tail_count
        current.append(sent)
        count += n
    if current:
        chunks.append(" ".join(current))
    return chunks


def build_chunks(files: list[Path], chunk_words: int, overlap: int) -> list[Chunk]:
    all_chunks = []
    for f in files:
        text = read_file(f)
        pieces = chunk_text(text, chunk_words, overlap)
        print(f"{f.name}: {len(text.split())} words -> {len(pieces)} chunks")
        all_chunks += [Chunk(f.name, i, t) for i, t in enumerate(pieces)]
    return all_chunks


# ----------------------------------------------------------------------------
# 3. Similarity checks
# ----------------------------------------------------------------------------
def embed_documents(model, texts):
    # EmbeddingGemma has dedicated document/query prompts (sentence-transformers >= 5.0)
    if hasattr(model, "encode_document"):
        return model.encode_document(texts, batch_size=16, show_progress_bar=True,
                                     convert_to_tensor=True)
    return model.encode(texts, batch_size=16, show_progress_bar=True, convert_to_tensor=True)


def embed_query(model, text):
    if hasattr(model, "encode_query"):
        return model.encode_query(text, convert_to_tensor=True)
    return model.encode(text, convert_to_tensor=True)


def search(model, chunks, doc_emb, query, top_k=5):
    q = embed_query(model, query)
    scores = model.similarity(q, doc_emb)[0]
    top = torch.topk(scores, k=min(top_k, len(chunks)))
    print(f'\n=== Top {len(top.indices)} chunks for query: "{query}" ===')
    for score, idx in zip(top.values, top.indices):
        c = chunks[int(idx)]
        print(f"\n[{score:.3f}] {c.source} (chunk {c.index})\n{c.text[:300]}...")


def similar_pairs(model, chunks, doc_emb, threshold=0.8, cross_file_only=True):
    sim = model.similarity(doc_emb, doc_emb)
    n = len(chunks)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if cross_file_only and chunks[i].source == chunks[j].source:
                continue
            s = float(sim[i, j])
            if s >= threshold:
                pairs.append((s, i, j))
    pairs.sort(reverse=True)
    print(f"\n=== {len(pairs)} chunk pairs with similarity >= {threshold} ===")
    for s, i, j in pairs[:20]:
        a, b = chunks[i], chunks[j]
        print(f"\n[{s:.3f}] {a.source}#{a.index}  <->  {b.source}#{b.index}")
        print(f"  A: {a.text[:150]}...")
        print(f"  B: {b.text[:150]}...")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="Files and/or folders (.txt .md .pdf .docx)")
    ap.add_argument("--chunk-words", type=int, default=200)
    ap.add_argument("--overlap", type=int, default=40)
    ap.add_argument("--query", type=str, help="Search query to compare against all chunks")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--pairs", action="store_true", help="Find similar chunks between files")
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--same-file", action="store_true", help="Also compare chunks within the same file")
    args = ap.parse_args()

    files = collect_files(args.inputs)
    if not files:
        raise SystemExit("No supported files found.")

    chunks = build_chunks(files, args.chunk_words, args.overlap)
    if not chunks:
        raise SystemExit("No text extracted (scanned PDF?).")

    model = SentenceTransformer("google/embeddinggemma-300m")
    doc_emb = embed_documents(model, [c.text for c in chunks])
    print(f"\nEmbeddings: {tuple(doc_emb.shape)}")

    if args.query:
        search(model, chunks, doc_emb, args.query, args.top_k)
    if args.pairs:
        similar_pairs(model, chunks, doc_emb, args.threshold, cross_file_only=not args.same_file)
    if not args.query and not args.pairs:
        print("\nNothing to compare - use --query and/or --pairs.")


if __name__ == "__main__":
    main()
