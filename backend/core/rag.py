"""
Domain C - the RAG layer.
 
Chunks a world bible into retrievable facts, embeds them locally, and hands
downstream generators a compact grounded context.
 
Two design decisions worth understanding before you change anything:
 
1. We chunk STRUCTURALLY, not by character count. We wrote the bible's JSON
   schema ourselves, so we know exactly where the semantic boundaries are.
   One faction = one chunk. One timeline event = one chunk. Sliding a 500-char
   window over the JSON would split a faction's goal from its name and
   retrieve garbage.
 
2. Every retrieval force-includes the "core" chunk (world name, pitch, tone).
   Semantic search on "what does a blacksmith sell" will never surface the
   world's tone by similarity, but every generator needs it in every prompt.
 
Embeddings run locally via Chroma's bundled MiniLM (ONNX, no torch, no API
calls, no quota). Free forever and works offline.
 
Usage:
    python -m core.rag output/bibles/your_world.json
"""
 
import os
import sys
import json
import shutil
from typing import Optional
 
import chromadb
 
CHROMA_DIR = "output/chroma"
 
 
# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------
 
def chunk_bible(bible: dict) -> list[dict]:
    """
    Turn a bible into a flat list of {id, text, metadata} chunks.
 
    Each chunk's text is written as a self-contained sentence, because that is
    what gets embedded. "The Ashen Choir wants to reopen the sealed vault" is
    retrievable; a bare JSON fragment is not.
    """
    chunks: list[dict] = []
    world = bible.get("world_name", "this world")
 
    def add(kind: str, key: str, text: str, **meta):
        chunks.append({
            "id": f"{kind}:{key}",
            "text": text.strip(),
            "metadata": {"type": kind, "name": key, **meta},
        })
 
    # --- core: always injected, never filtered out ---
    core = (
        f"{world}. {bible.get('one_line_pitch', '')} "
        f"Tone: {', '.join(bible.get('tone', []))}. "
        f"Genre: {', '.join(bible.get('genre_tags', []))}."
    )
    add("core", "identity", core)
 
    # --- factions ---
    for f in bible.get("factions", []):
        if not isinstance(f, dict):
            continue
        name = f.get("name", "unnamed faction")
        add("faction", name,
            f"{name} is a faction in {world}. Their goal is {f.get('goal', 'unknown')}. "
            f"They pursue it by {f.get('methods', 'unknown means')}. "
            f"They are based at {f.get('seat_of_power', 'no fixed seat')}. "
            f"Toward the player they are {f.get('attitude_to_player', 'neutral')}.",
            seat=str(f.get("seat_of_power", "")))
 
    # --- places ---
    for p in bible.get("geography", []):
        if not isinstance(p, dict):
            continue
        name = p.get("name", "unnamed place")
        add("place", name,
            f"{name} is a {p.get('type', 'location')} in {world}. "
            f"{p.get('description', '')} "
            f"Danger level {p.get('danger_level', '?')} out of 5.",
            danger=int(p.get("danger_level") or 0))
 
    # --- timeline ---
    for i, e in enumerate(bible.get("timeline", [])):
        if not isinstance(e, dict):
            continue
        add("event", f"{i:02d}_{e.get('era', 'era')}",
            f"About {e.get('years_ago', '?')} years ago, during the "
            f"{e.get('era', 'unnamed era')}: {e.get('event', '')}",
            years_ago=int(e.get("years_ago") or 0))
 
    # --- rules ---
    for i, r in enumerate(bible.get("magic_or_tech_rules", [])):
        if isinstance(r, dict):
            text = f"Rule of {world}: {r.get('rule', '')} The cost is {r.get('cost', 'unspecified')}."
        else:
            text = f"Rule of {world}: {r}"
        add("rule", f"{i:02d}", text)
 
    # --- conflicts ---
    for i, c in enumerate(bible.get("conflicts", [])):
        if isinstance(c, dict):
            text = (f"Ongoing conflict in {world}: {c.get('summary', '')} "
                    f"Between {' and '.join(c.get('parties', []))}. "
                    f"At stake: {c.get('stakes', '')}")
        else:
            text = f"Ongoing conflict in {world}: {c}"
        add("conflict", f"{i:02d}", text)
 
    # --- visual style: one chunk, consumed almost entirely by domain E ---
    vs = bible.get("visual_style", {}) or {}
    add("visual", "style",
        f"Visual direction for {world}. "
        f"Palette: {', '.join(vs.get('palette', []))}. "
        f"Lighting: {vs.get('lighting', '')}. "
        f"Architecture: {vs.get('architecture', '')}. "
        f"Materials: {', '.join(vs.get('materials', []))}. "
        f"Keywords: {', '.join(vs.get('art_direction_keywords', []))}.")
 
    return [c for c in chunks if len(c["text"]) > 20]
 
 
# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------
 
class WorldMemory:
    """A vector store scoped to a single generated world."""
 
    def __init__(self, world_id: str, persist_dir: str = CHROMA_DIR):
        self.world_id = world_id
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=f"world_{world_id}",
            metadata={"hnsw:space": "cosine"},
        )
        self._core_text: Optional[str] = None
 
    # -- build -------------------------------------------------------------
 
    @classmethod
    def from_bible(cls, bible: dict, persist_dir: str = CHROMA_DIR,
                   rebuild: bool = True) -> "WorldMemory":
        from .world_bible import slug
 
        mem = cls(slug(bible.get("world_name")), persist_dir)
        if rebuild:
            mem.reset()
        mem.index(bible)
        return mem
 
    def reset(self):
        try:
            self.client.delete_collection(f"world_{self.world_id}")
        except Exception:  # noqa: BLE001 - collection may not exist yet
            pass
        self.collection = self.client.get_or_create_collection(
            name=f"world_{self.world_id}",
            metadata={"hnsw:space": "cosine"},
        )
 
    def index(self, bible: dict) -> int:
        chunks = chunk_bible(bible)
        self.collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            metadatas=[c["metadata"] for c in chunks],
        )
        for c in chunks:
            if c["metadata"]["type"] == "core":
                self._core_text = c["text"]
        return len(chunks)
 
    # -- read --------------------------------------------------------------
 
    def _core(self) -> str:
        if self._core_text is None:
            got = self.collection.get(ids=["core:identity"])
            docs = got.get("documents") or []
            self._core_text = docs[0] if docs else ""
        return self._core_text
 
    def retrieve(self, query: str, k: int = 6,
                 types: Optional[list[str]] = None) -> list[dict]:
        """
        Semantic search over the bible. `types` filters to specific chunk
        kinds, e.g. types=["place", "faction"] when generating a map.
        """
        where = {"type": {"$in": types}} if types else None
        n = min(k, max(self.collection.count(), 1))
        res = self.collection.query(query_texts=[query], n_results=n, where=where)
 
        out = []
        for doc, meta, dist in zip(
            res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            out.append({"text": doc, "metadata": meta, "distance": dist})
        return out
 
    def context(self, query: str, k: int = 6,
                types: Optional[list[str]] = None) -> str:
        """
        The method every generator actually calls. Returns a formatted block
        ready to paste into a prompt, with the core identity always on top.
        """
        hits = self.retrieve(query, k=k, types=types)
        lines = [f"[world] {self._core()}"]
        for h in hits:
            if h["metadata"].get("type") == "core":
                continue
            lines.append(f"[{h['metadata']['type']}] {h['text']}")
        return "\n".join(lines)
 
 
GROUNDING_PREAMBLE = """You are writing content for an existing game world.
The CANON block below is the only source of truth. Obey it exactly:
never contradict a date, name, faction goal or place description in it.
If the canon does not cover something you need, invent it in a way that is
consistent with the canon's tone, and do not contradict anything stated.
 
CANON:
{context}
"""
 
 
def grounded_prompt(memory: WorldMemory, query: str, task: str,
                    k: int = 6, types: Optional[list[str]] = None) -> str:
    """Convenience wrapper: canon block + the actual instruction."""
    ctx = memory.context(query, k=k, types=types)
    return GROUNDING_PREAMBLE.format(context=ctx) + "\n\nTASK:\n" + task
 
 
if __name__ == "__main__":
    from .world_bible import load_bible
 
    if len(sys.argv) < 2:
        print("usage: python -m core.rag path/to/bible.json")
        raise SystemExit(1)
 
    bible = load_bible(sys.argv[1])
    memory = WorldMemory.from_bible(bible)
    print(f"indexed {memory.collection.count()} chunks "
          f"for {bible.get('world_name')}\n")
 
    for q in ["a dangerous place to send a low level player",
              "who would hire an assassin",
              "what art style should the concept art use"]:
        print(f"--- query: {q}")
        print(memory.context(q, k=4))
        print()
 