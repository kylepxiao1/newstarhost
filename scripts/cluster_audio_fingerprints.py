#!/usr/bin/env python3
"""
Find topic_trends rows that are likely using the same audio.

This script consumes fingerprints written by:
  scripts/find_viral_trends.py

Expected fingerprint JSON shape (audio_fingerprint column):
{
  "version": "landmark_v1",
  "simhash64": "8b2b5e5bb53ade29",
  "hashes": ["0123abcd89ef0011:42", ...]
}
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


def configure_stdout_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_console_text(value: Any) -> str:
    text = str(value or "")
    try:
        text.encode(sys.stdout.encoding or "utf-8", errors="strict")
        return text
    except Exception:
        return text.encode("ascii", errors="replace").decode("ascii")


def load_env_file_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values.setdefault(key, value)
    return values


def first_non_empty(candidates: Iterable[str]) -> str:
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def resolve_supabase_url_and_key(
    env_file: Path,
    url_override: str,
    project_id_override: str,
    key_override: str,
) -> Tuple[Optional[str], Optional[str]]:
    env_values = load_env_file_values(env_file)

    url = first_non_empty(
        [
            url_override,
            os.environ.get("SUPABASE_URL", ""),
            env_values.get("SUPABASE_URL", ""),
        ]
    )
    project_id = first_non_empty(
        [
            project_id_override,
            os.environ.get("SUPABASE_PROJECT_ID", ""),
            env_values.get("SUPABASE_PROJECT_ID", ""),
        ]
    )
    if not url and project_id:
        url = f"https://{project_id}.supabase.co"

    key = first_non_empty(
        [
            key_override,
            os.environ.get("SUPABASE_SECRET_KEY", ""),
            env_values.get("SUPABASE_SECRET_KEY", ""),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            env_values.get("SUPABASE_SERVICE_ROLE_KEY", ""),
            os.environ.get("SUPABASE_API_KEY", ""),
            env_values.get("SUPABASE_API_KEY", ""),
        ]
    )
    return (url or None), (key or None)


def extract_missing_supabase_column(response: requests.Response) -> Optional[str]:
    if response.status_code < 400:
        return None
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
    except Exception:
        message = ""
    if not message:
        message = str(response.text or "")

    lowered = message.lower()
    if "column " in lowered and " does not exist" in lowered:
        if '"' in message:
            parts = message.split('"')
            if len(parts) >= 2 and parts[1].strip():
                return parts[1].strip()
        tail = message.split("column", 1)[1].split("does not exist", 1)[0].strip()
        if "." in tail:
            tail = tail.split(".")[-1]
        return tail.strip().strip('"')

    if "schema cache" not in lowered:
        return None
    marker = "Could not find the '"
    if marker not in message:
        return None
    tail = message.split(marker, 1)[1]
    column = tail.split("'", 1)[0].strip()
    return column or None


def explain_http_error(response: requests.Response) -> str:
    text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        text = str(response.text or "")
    text = text.replace("\n", " ").strip()
    if len(text) > 320:
        text = text[:317] + "..."
    return f"status={response.status_code} body={safe_console_text(text)}"


def build_cluster_columns_sql(table_name: str, cluster_id_column: str, shared_column: str) -> str:
    table = str(table_name or "topic_trends").strip()
    cluster_col = str(cluster_id_column or "cluster_id").strip()
    shared_col = str(shared_column or "shared").strip()
    return (
        f"alter table public.{table}\n"
        f"add column if not exists {cluster_col} integer;\n\n"
        f"alter table public.{table}\n"
        f"alter column {cluster_col} type integer\n"
        f"using nullif(regexp_replace(coalesce({cluster_col}::text, ''), '[^0-9-]', '', 'g'), '')::integer;\n\n"
        f"alter table public.{table}\n"
        f"add column if not exists {shared_col} boolean;\n\n"
        f"alter table public.{table}\n"
        f"alter column {shared_col} set default true;\n\n"
        f"update public.{table}\n"
        f"set {shared_col} = true\n"
        f"where {shared_col} is null;"
    )


@dataclass
class TrendAudioRow:
    row_id: Any
    topic: str
    song: str
    artist: str
    song_link: str
    hash_weights: Dict[int, int]
    hash_weight_total: int
    simhash64: Optional[int]

    @property
    def label(self) -> str:
        left = f"{self.song} - {self.artist}".strip(" -")
        if not left:
            left = "(no song/artist)"
        return left


class DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def parse_simhash64(raw: Any) -> Optional[int]:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text.startswith("0x"):
        text = text[2:]
    try:
        return int(text, 16)
    except Exception:
        return None


def parse_hash_entry(raw: Any) -> Optional[Tuple[int, int]]:
    text = str(raw or "").strip()
    if not text or ":" not in text:
        return None
    left, right = text.split(":", 1)
    left = left.strip().lower()
    right = right.strip()
    if left.startswith("0x"):
        left = left[2:]
    try:
        hashed = int(left, 16)
    except Exception:
        return None
    try:
        weight = int(float(right))
    except Exception:
        return None
    if weight <= 0:
        return None
    return hashed, weight


def parse_fingerprint(raw: Any) -> Optional[Tuple[Dict[int, int], Optional[int]]]:
    if not isinstance(raw, dict):
        return None
    hashes_raw = raw.get("hashes")
    if not isinstance(hashes_raw, list):
        return None

    hash_weights: Dict[int, int] = {}
    for item in hashes_raw:
        parsed = parse_hash_entry(item)
        if not parsed:
            continue
        hashed, weight = parsed
        hash_weights[hashed] = hash_weights.get(hashed, 0) + weight
    if not hash_weights:
        return None

    simhash = parse_simhash64(raw.get("simhash64"))
    return hash_weights, simhash


def ensure_columns_exist(
    session: requests.Session,
    endpoint: str,
    required_columns: List[str],
    timeout_seconds: float,
) -> None:
    params = {"select": ",".join(required_columns), "limit": "1"}
    resp = session.get(endpoint, params=params, timeout=timeout_seconds)
    if resp.status_code < 400:
        return
    missing = extract_missing_supabase_column(resp)
    if missing:
        raise RuntimeError(f"Supabase column missing: {missing}")
    raise RuntimeError(f"Supabase query failed: {explain_http_error(resp)}")


def fetch_rows_page(
    session: requests.Session,
    endpoint: str,
    *,
    id_column: str,
    topic_column: str,
    song_column: str,
    artist_column: str,
    url_column: str,
    fingerprint_column: str,
    generated_at_column: str,
    generated_since_iso: str,
    batch_size: int,
    last_seen_id: Optional[Any],
    timeout_seconds: float,
    topic_filter: str,
) -> List[Dict[str, Any]]:
    select_cols = [
        id_column,
        topic_column,
        song_column,
        artist_column,
        url_column,
        fingerprint_column,
    ]
    params: Dict[str, str] = {
        "select": ",".join(select_cols),
        "order": f"{id_column}.asc",
        "limit": str(max(1, batch_size)),
        fingerprint_column: "not.is.null",
    }
    if last_seen_id is not None:
        params[id_column] = f"gt.{last_seen_id}"
    if topic_filter:
        params[topic_column] = f"eq.{topic_filter}"
    if generated_since_iso:
        params[generated_at_column] = f"gte.{generated_since_iso}"

    resp = session.get(endpoint, params=params, timeout=timeout_seconds)
    if resp.status_code >= 400:
        raise RuntimeError(f"Failed to fetch rows: {explain_http_error(resp)}")
    payload = resp.json()
    if not isinstance(payload, list):
        raise RuntimeError("Supabase fetch returned non-list payload.")
    return payload


def hamming_distance_64(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None or b is None:
        return None
    return int((a ^ b).bit_count())


def build_rows_from_payload(payload_rows: List[Dict[str, Any]], args: argparse.Namespace) -> Tuple[List[TrendAudioRow], int]:
    rows: List[TrendAudioRow] = []
    skipped_invalid = 0
    for raw in payload_rows:
        parsed = parse_fingerprint(raw.get(args.fingerprint_column))
        if not parsed:
            skipped_invalid += 1
            continue
        hash_weights, simhash = parsed
        total = int(sum(hash_weights.values()))
        if total <= 0:
            skipped_invalid += 1
            continue

        rows.append(
            TrendAudioRow(
                row_id=raw.get(args.id_column),
                topic=str(raw.get(args.topic_column) or "").strip(),
                song=str(raw.get(args.song_column) or "").strip(),
                artist=str(raw.get(args.artist_column) or "").strip(),
                song_link=str(raw.get(args.url_column) or "").strip(),
                hash_weights=hash_weights,
                hash_weight_total=total,
                simhash64=simhash,
            )
        )
    return rows, skipped_invalid


def weighted_intersection(weights_a: Dict[int, int], weights_b: Dict[int, int]) -> Tuple[int, int]:
    if len(weights_a) > len(weights_b):
        weights_a, weights_b = weights_b, weights_a
    inter_weight = 0
    inter_hashes = 0
    for hashed, wa in weights_a.items():
        wb = weights_b.get(hashed)
        if wb is None:
            continue
        inter_weight += min(wa, wb)
        inter_hashes += 1
    return inter_weight, inter_hashes


def confidence_from_metrics(
    weighted_jaccard: float,
    overlap: float,
    simhash_hamming: Optional[int],
) -> float:
    if simhash_hamming is None:
        simhash_score = 0.5
    else:
        simhash_score = max(0.0, 1.0 - (float(simhash_hamming) / 64.0))
    return (0.7 * weighted_jaccard) + (0.2 * overlap) + (0.1 * simhash_score)


def classify_match(confidence: float, weighted_jaccard: float, simhash_hamming: Optional[int]) -> str:
    if confidence >= 0.72 and weighted_jaccard >= 0.50 and (simhash_hamming is None or simhash_hamming <= 12):
        return "high"
    if confidence >= 0.55 and weighted_jaccard >= 0.30 and (simhash_hamming is None or simhash_hamming <= 20):
        return "medium"
    return "low"


def build_candidate_pairs(rows: List[TrendAudioRow], args: argparse.Namespace) -> Tuple[Dict[Tuple[int, int], int], Dict[Tuple[int, int], int], int]:
    hash_df: Counter = Counter()
    top_hashes_per_row: List[List[Tuple[int, int]]] = []
    for row in rows:
        ordered = sorted(row.hash_weights.items(), key=lambda item: item[1], reverse=True)
        if args.max_hashes_per_row > 0:
            ordered = ordered[: args.max_hashes_per_row]
        top_hashes_per_row.append(ordered)
        for hashed, _ in ordered:
            hash_df[hashed] += 1

    max_df_abs = max(2, int(len(rows) * max(0.0, min(1.0, args.max_hash_docfreq_ratio))))

    postings: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for row_idx, entries in enumerate(top_hashes_per_row):
        for hashed, weight in entries:
            if hash_df[hashed] > max_df_abs:
                continue
            postings[hashed].append((row_idx, weight))

    pair_shared_weight: Dict[Tuple[int, int], int] = defaultdict(int)
    pair_shared_hashes: Dict[Tuple[int, int], int] = defaultdict(int)
    skipped_due_to_topic = 0

    for posting in postings.values():
        if len(posting) < 2:
            continue
        posting.sort(key=lambda entry: entry[0])
        for left_index in range(len(posting) - 1):
            a_idx, a_weight = posting[left_index]
            for right_index in range(left_index + 1, len(posting)):
                b_idx, b_weight = posting[right_index]
                if args.same_topic_only and rows[a_idx].topic != rows[b_idx].topic:
                    skipped_due_to_topic += 1
                    continue
                key = (a_idx, b_idx)
                pair_shared_weight[key] += min(a_weight, b_weight)
                pair_shared_hashes[key] += 1

    return pair_shared_weight, pair_shared_hashes, skipped_due_to_topic


def score_matches(rows: List[TrendAudioRow], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if len(rows) < 2:
        return [], {"candidate_pairs": 0, "pruned_pairs": 0, "same_topic_pairs_skipped": 0}

    pair_shared_weight, pair_shared_hashes, skipped_due_to_topic = build_candidate_pairs(rows, args)
    candidate_pairs = len(pair_shared_weight)
    pruned_pairs = 0
    matches: List[Dict[str, Any]] = []

    for (a_idx, b_idx), shared_weight in pair_shared_weight.items():
        shared_hashes = pair_shared_hashes.get((a_idx, b_idx), 0)
        if shared_hashes < args.min_shared_hashes:
            pruned_pairs += 1
            continue
        if shared_weight < args.min_shared_weight:
            pruned_pairs += 1
            continue

        row_a = rows[a_idx]
        row_b = rows[b_idx]

        # Recompute exact intersection on full parsed hash maps for final score stability.
        exact_inter_weight, exact_inter_hashes = weighted_intersection(row_a.hash_weights, row_b.hash_weights)
        if exact_inter_hashes < args.min_shared_hashes:
            pruned_pairs += 1
            continue
        if exact_inter_weight < args.min_shared_weight:
            pruned_pairs += 1
            continue

        union_weight = row_a.hash_weight_total + row_b.hash_weight_total - exact_inter_weight
        if union_weight <= 0:
            pruned_pairs += 1
            continue

        weighted_jaccard = float(exact_inter_weight) / float(union_weight)
        overlap = float(exact_inter_weight) / float(max(1, min(row_a.hash_weight_total, row_b.hash_weight_total)))
        if weighted_jaccard < args.min_weighted_jaccard:
            pruned_pairs += 1
            continue
        if overlap < args.min_overlap:
            pruned_pairs += 1
            continue

        simhash_hamming = hamming_distance_64(row_a.simhash64, row_b.simhash64)
        if args.max_simhash_hamming >= 0 and simhash_hamming is not None and simhash_hamming > args.max_simhash_hamming:
            pruned_pairs += 1
            continue

        confidence = confidence_from_metrics(
            weighted_jaccard=weighted_jaccard,
            overlap=overlap,
            simhash_hamming=simhash_hamming,
        )
        if confidence < args.min_confidence:
            pruned_pairs += 1
            continue

        match = {
            "confidence": round(confidence, 6),
            "confidence_label": classify_match(confidence, weighted_jaccard, simhash_hamming),
            "weighted_jaccard": round(weighted_jaccard, 6),
            "overlap": round(overlap, 6),
            "shared_hash_weight": int(exact_inter_weight),
            "shared_hashes": int(exact_inter_hashes),
            "simhash_hamming": simhash_hamming,
            "a": {
                "id": row_a.row_id,
                "topic": row_a.topic,
                "song": row_a.song,
                "artist": row_a.artist,
                "song_link": row_a.song_link,
            },
            "b": {
                "id": row_b.row_id,
                "topic": row_b.topic,
                "song": row_b.song,
                "artist": row_b.artist,
                "song_link": row_b.song_link,
            },
            "_a_idx": a_idx,
            "_b_idx": b_idx,
        }
        matches.append(match)

    matches.sort(
        key=lambda item: (
            float(item["confidence"]),
            float(item["weighted_jaccard"]),
            int(item["shared_hash_weight"]),
            int(item["shared_hashes"]),
        ),
        reverse=True,
    )

    return matches, {
        "candidate_pairs": candidate_pairs,
        "pruned_pairs": pruned_pairs,
        "same_topic_pairs_skipped": skipped_due_to_topic,
    }


def build_clusters(rows: List[TrendAudioRow], matches: List[Dict[str, Any]], min_edge_confidence: float) -> List[Dict[str, Any]]:
    if not rows or not matches:
        return []

    dsu = DSU(len(rows))
    for match in matches:
        confidence = float(match.get("confidence") or 0.0)
        if confidence < min_edge_confidence:
            continue
        a_idx = int(match.get("_a_idx"))
        b_idx = int(match.get("_b_idx"))
        dsu.union(a_idx, b_idx)

    groups: Dict[int, List[int]] = defaultdict(list)
    for idx in range(len(rows)):
        groups[dsu.find(idx)].append(idx)

    clusters: List[Dict[str, Any]] = []
    for member_indices in groups.values():
        if len(member_indices) < 2:
            continue

        member_set = set(member_indices)
        edge_scores: List[float] = []
        for match in matches:
            a_idx = int(match.get("_a_idx"))
            b_idx = int(match.get("_b_idx"))
            if a_idx in member_set and b_idx in member_set:
                edge_scores.append(float(match.get("confidence") or 0.0))

        members_payload = []
        for idx in member_indices:
            row = rows[idx]
            members_payload.append(
                {
                    "id": row.row_id,
                    "topic": row.topic,
                    "song": row.song,
                    "artist": row.artist,
                    "song_link": row.song_link,
                }
            )

        clusters.append(
            {
                "size": len(member_indices),
                "avg_edge_confidence": round(sum(edge_scores) / len(edge_scores), 6) if edge_scores else None,
                "members": members_payload,
            }
        )

    clusters.sort(
        key=lambda item: (int(item["size"]), float(item["avg_edge_confidence"] or 0.0)),
        reverse=True,
    )
    return clusters


def _coerce_sortable_id(value: Any) -> Tuple[int, Any]:
    try:
        return (0, int(value))
    except Exception:
        return (1, str(value))


def assign_cluster_ids(
    clusters: List[Dict[str, Any]],
    start_cluster_id: int = 1,
) -> Tuple[List[Dict[str, Any]], Dict[Any, int]]:
    indexed_clusters: List[Tuple[Tuple[int, Any], Dict[str, Any]]] = []
    for cluster in clusters:
        members = cluster.get("members") if isinstance(cluster, dict) else None
        if not isinstance(members, list) or not members:
            continue
        min_member_id = min((m.get("id") for m in members if isinstance(m, dict)), key=_coerce_sortable_id)
        indexed_clusters.append((_coerce_sortable_id(min_member_id), cluster))

    indexed_clusters.sort(key=lambda item: item[0])
    assignments: Dict[Any, int] = {}
    ordered_clusters: List[Dict[str, Any]] = []

    next_id = max(1, int(start_cluster_id))
    for _, cluster in indexed_clusters:
        cluster_id = int(next_id)
        next_id += 1
        members = cluster.get("members") if isinstance(cluster, dict) else None
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            row_id = member.get("id")
            if row_id is None:
                continue
            member["cluster_id"] = cluster_id
            assignments[row_id] = cluster_id
        cluster["cluster_id"] = cluster_id
        ordered_clusters.append(cluster)

    return ordered_clusters, assignments


def patch_row_cluster_fields(
    session: requests.Session,
    endpoint: str,
    *,
    id_column: str,
    row_id: Any,
    cluster_id_column: str,
    shared_column: str,
    cluster_id: int,
    timeout_seconds: float,
) -> None:
    payload = {
        cluster_id_column: cluster_id,
        shared_column: True,
    }
    params = {id_column: f"eq.{row_id}"}
    resp = session.patch(endpoint, params=params, json=payload, timeout=timeout_seconds)
    if resp.status_code in {200, 201, 204}:
        return
    raise RuntimeError(
        f"Failed to patch row id={safe_console_text(row_id)} with cluster assignment: {explain_http_error(resp)}"
    )


def to_postgrest_in_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        as_int = int(value)
        if float(as_int) == float(value):
            return str(as_int)
    text = str(value or "").strip()
    if re.fullmatch(r"-?\d+", text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def clear_unassigned_cluster_ids(
    session: requests.Session,
    endpoint: str,
    *,
    id_column: str,
    cluster_id_column: str,
    row_ids_to_clear: List[Any],
    timeout_seconds: float,
    batch_size: int = 200,
) -> int:
    if not row_ids_to_clear:
        return 0

    updated = 0
    size = max(1, int(batch_size))
    for start in range(0, len(row_ids_to_clear), size):
        batch = row_ids_to_clear[start : start + size]
        in_clause = ",".join(to_postgrest_in_literal(value) for value in batch)
        params = {
            id_column: f"in.({in_clause})",
            cluster_id_column: "not.is.null",
        }
        payload = {cluster_id_column: None}
        resp = session.patch(endpoint, params=params, json=payload, timeout=timeout_seconds)
        if resp.status_code not in {200, 201, 204}:
            raise RuntimeError(
                "Failed to clear unassigned cluster ids: "
                + explain_http_error(resp)
            )
        updated += len(batch)
    return updated


def apply_cluster_assignments(
    session: requests.Session,
    endpoint: str,
    *,
    id_column: str,
    cluster_id_column: str,
    shared_column: str,
    assignments: Dict[Any, int],
    timeout_seconds: float,
) -> int:
    updated = 0
    for row_id, cluster_id in assignments.items():
        patch_row_cluster_fields(
            session=session,
            endpoint=endpoint,
            id_column=id_column,
            row_id=row_id,
            cluster_id_column=cluster_id_column,
            shared_column=shared_column,
            cluster_id=cluster_id,
            timeout_seconds=timeout_seconds,
        )
        updated += 1
    return updated


def print_top_matches(matches: List[Dict[str, Any]], limit: int) -> None:
    if not matches:
        print("No likely same-audio pairs found with current thresholds.")
        return
    top_n = min(max(1, limit), len(matches))
    print(f"Top {top_n} likely same-audio pairs:")
    for idx, match in enumerate(matches[:top_n], start=1):
        a = match["a"]
        b = match["b"]
        print(
            f"{idx:>3}. conf={float(match['confidence']):.3f} "
            f"jac={float(match['weighted_jaccard']):.3f} "
            f"ov={float(match['overlap']):.3f} "
            f"hamming={match['simhash_hamming']} "
            f"shared_w={int(match['shared_hash_weight'])} "
            f"ids={a['id']} <-> {b['id']}"
        )
        print(
            f"     A: {safe_console_text(str(a.get('song') or '').strip())} | "
            f"{safe_console_text(str(a.get('artist') or '').strip())}"
        )
        print(
            f"     B: {safe_console_text(str(b.get('song') or '').strip())} | "
            f"{safe_console_text(str(b.get('artist') or '').strip())}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find topic_trends rows likely using the same audio based on "
            "audio_fingerprint landmark hashes."
        )
    )
    parser.add_argument("--table", default="topic_trends", help="Supabase table to scan.")
    parser.add_argument("--id-column", default="id", help="Primary key column.")
    parser.add_argument("--topic-column", default="topic", help="Topic column.")
    parser.add_argument("--song-column", default="song", help="Song title column.")
    parser.add_argument("--artist-column", default="artist", help="Artist column.")
    parser.add_argument("--url-column", default="song_link", help="Song/video URL column.")
    parser.add_argument("--fingerprint-column", default="audio_fingerprint", help="Fingerprint JSONB column.")
    parser.add_argument("--generated-at-column", default="generated_at", help="Generated/upload timestamp column.")
    parser.add_argument("--cluster-id-column", default="cluster_id", help="Cluster id destination column.")
    parser.add_argument("--shared-column", default="shared", help="Shared-flag destination column.")
    parser.add_argument("--topic", default="", help="Optional exact topic filter.")
    parser.add_argument("--same-topic-only", action="store_true", help="Only compare rows with identical topic.")
    parser.add_argument(
        "--since-days",
        type=float,
        default=0.0,
        help="Only include rows with generated_at >= now - N days (0 disables).",
    )
    parser.add_argument(
        "--cluster-id-start-at",
        type=int,
        default=1,
        help="Assign cluster IDs starting at this integer value.",
    )

    parser.add_argument("--batch-size", type=int, default=500, help="Rows fetched per Supabase page.")
    parser.add_argument("--max-rows", type=int, default=0, help="Max fingerprinted rows to load (0 = no limit).")
    parser.add_argument("--max-hashes-per-row", type=int, default=256, help="Top N hashes per row for candidate generation.")
    parser.add_argument(
        "--max-hash-docfreq-ratio",
        type=float,
        default=0.25,
        help="Ignore hashes appearing in more than this ratio of rows.",
    )

    parser.add_argument("--min-shared-weight", type=int, default=80, help="Minimum shared hash weight.")
    parser.add_argument("--min-shared-hashes", type=int, default=10, help="Minimum number of shared hashes.")
    parser.add_argument("--min-weighted-jaccard", type=float, default=0.12, help="Minimum weighted Jaccard similarity.")
    parser.add_argument("--min-overlap", type=float, default=0.18, help="Minimum overlap coefficient.")
    parser.add_argument(
        "--max-simhash-hamming",
        type=int,
        default=26,
        help="Maximum allowed simhash hamming distance (-1 disables this filter).",
    )
    parser.add_argument("--min-confidence", type=float, default=0.35, help="Minimum final confidence score.")
    parser.add_argument(
        "--cluster-min-confidence",
        type=float,
        default=0.45,
        help="Minimum confidence edge to connect cluster members.",
    )

    parser.add_argument("--top", type=int, default=50, help="How many top pairs to print.")
    parser.add_argument(
        "--no-write-cluster-updates",
        action="store_true",
        help="Do not write cluster_id/shared updates back to Supabase.",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional output JSON path for full results.",
    )

    parser.add_argument("--supabase-url", default="", help="Override Supabase URL.")
    parser.add_argument("--supabase-project-id", default="", help="Override Supabase project ID.")
    parser.add_argument("--supabase-key", default="", help="Override Supabase secret/service key.")
    parser.add_argument("--env-file", default="app.env", help="Env file used for fallback credentials.")
    parser.add_argument("--http-timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    return parser.parse_args()


def main() -> None:
    configure_stdout_utf8()
    args = parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env_file = Path(args.env_file)
    if not env_file.is_absolute():
        env_file = (repo_root / env_file).resolve()

    supabase_url, supabase_key = resolve_supabase_url_and_key(
        env_file=env_file,
        url_override=args.supabase_url,
        project_id_override=args.supabase_project_id,
        key_override=args.supabase_key,
    )
    if not supabase_url or not supabase_key:
        raise SystemExit(
            "Missing Supabase credentials. Set SUPABASE_URL/SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY "
            "or pass --supabase-url --supabase-key."
        )

    endpoint = supabase_url.rstrip("/") + f"/rest/v1/{args.table}"

    session = requests.Session()
    session.headers.update(
        {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    required_columns = [
        args.id_column,
        args.topic_column,
        args.song_column,
        args.artist_column,
        args.url_column,
        args.fingerprint_column,
    ]
    if float(args.since_days) > 0:
        required_columns.append(args.generated_at_column)
    write_cluster_updates = not bool(args.no_write_cluster_updates)
    if write_cluster_updates:
        required_columns.extend([args.cluster_id_column, args.shared_column])
    try:
        ensure_columns_exist(
            session=session,
            endpoint=endpoint,
            required_columns=required_columns,
            timeout_seconds=max(1.0, args.http_timeout),
        )
    except RuntimeError as exc:
        message = str(exc)
        if "Supabase column missing" in message and args.fingerprint_column in message:
            print(message)
            print(
                "Create/fill the fingerprint column first, then rerun:\n"
                f"alter table public.{args.table}\n"
                f"add column if not exists {args.fingerprint_column} jsonb;"
            )
            raise SystemExit(2)
        if write_cluster_updates and (
            f"Supabase column missing: {args.cluster_id_column}" in message
            or f"Supabase column missing: {args.shared_column}" in message
        ):
            print(message)
            print("Create the cluster/shared columns, then rerun:")
            print(build_cluster_columns_sql(args.table, args.cluster_id_column, args.shared_column))
            raise SystemExit(2)
        raise

    print(
        f"[{now_iso()}] Loading fingerprint rows from {args.table} "
        f"(topic_filter={safe_console_text(args.topic or '*')})"
    )
    generated_since_iso = ""
    if float(args.since_days) > 0:
        generated_since_iso = (datetime.now(timezone.utc) - timedelta(days=float(args.since_days))).isoformat()
        print(
            f"[{now_iso()}] Applying generated_at window: {args.generated_at_column} >= {generated_since_iso}"
        )

    raw_rows: List[Dict[str, Any]] = []
    last_seen_id: Optional[Any] = None
    while True:
        if args.max_rows > 0 and len(raw_rows) >= args.max_rows:
            break
        remaining = args.max_rows - len(raw_rows) if args.max_rows > 0 else args.batch_size
        page_size = min(max(1, args.batch_size), max(1, remaining))

        page = fetch_rows_page(
            session=session,
            endpoint=endpoint,
            id_column=args.id_column,
            topic_column=args.topic_column,
            song_column=args.song_column,
            artist_column=args.artist_column,
            url_column=args.url_column,
            fingerprint_column=args.fingerprint_column,
            generated_at_column=args.generated_at_column,
            generated_since_iso=generated_since_iso,
            batch_size=page_size,
            last_seen_id=last_seen_id,
            timeout_seconds=max(1.0, args.http_timeout),
            topic_filter=str(args.topic or "").strip(),
        )
        if not page:
            break
        raw_rows.extend(page)
        page_last_id = page[-1].get(args.id_column)
        if page_last_id is None:
            raise RuntimeError(
                f"Fetched row page missing '{args.id_column}' on last row; cannot continue keyset paging."
            )
        last_seen_id = page_last_id

    if args.max_rows > 0 and len(raw_rows) > args.max_rows:
        raw_rows = raw_rows[: args.max_rows]

    rows, skipped_invalid = build_rows_from_payload(raw_rows, args)
    print(
        f"[{now_iso()}] Loaded rows={len(raw_rows)} usable_fingerprints={len(rows)} "
        f"invalid_or_unparseable={skipped_invalid}"
    )
    if len(rows) < 2:
        print("Need at least 2 usable fingerprints to compare.")
        return

    matches, stats = score_matches(rows, args)
    clusters = build_clusters(
        rows=rows,
        matches=matches,
        min_edge_confidence=max(0.0, args.cluster_min_confidence),
    )
    clusters, cluster_assignments = assign_cluster_ids(
        clusters,
        start_cluster_id=max(1, int(args.cluster_id_start_at)),
    )

    cluster_rows_updated = 0
    cluster_rows_cleared = 0
    if write_cluster_updates and cluster_assignments:
        assigned_ids = set(cluster_assignments.keys())
        unassigned_ids = [
            raw.get(args.id_column)
            for raw in raw_rows
            if raw.get(args.id_column) is not None and raw.get(args.id_column) not in assigned_ids
        ]
        cluster_rows_cleared = clear_unassigned_cluster_ids(
            session=session,
            endpoint=endpoint,
            id_column=args.id_column,
            cluster_id_column=args.cluster_id_column,
            row_ids_to_clear=unassigned_ids,
            timeout_seconds=max(1.0, args.http_timeout),
        )

        cluster_rows_updated = apply_cluster_assignments(
            session=session,
            endpoint=endpoint,
            id_column=args.id_column,
            cluster_id_column=args.cluster_id_column,
            shared_column=args.shared_column,
            assignments=cluster_assignments,
            timeout_seconds=max(1.0, args.http_timeout),
        )
    elif write_cluster_updates and not cluster_assignments:
        # No clusters found in this run: clear cluster_id for loaded rows.
        all_loaded_ids = [raw.get(args.id_column) for raw in raw_rows if raw.get(args.id_column) is not None]
        cluster_rows_cleared = clear_unassigned_cluster_ids(
            session=session,
            endpoint=endpoint,
            id_column=args.id_column,
            cluster_id_column=args.cluster_id_column,
            row_ids_to_clear=all_loaded_ids,
            timeout_seconds=max(1.0, args.http_timeout),
        )

    print(
        f"[{now_iso()}] Matching complete: candidate_pairs={stats['candidate_pairs']} "
        f"pruned_pairs={stats['pruned_pairs']} matched_pairs={len(matches)} "
        f"clusters={len(clusters)} cluster_rows_updated={cluster_rows_updated} "
        f"cluster_rows_cleared={cluster_rows_cleared}"
    )
    if args.same_topic_only:
        print(f"[{now_iso()}] Pairs skipped by same-topic-only filter: {stats['same_topic_pairs_skipped']}")

    print_top_matches(matches, limit=args.top)

    if args.output_json:
        output_path = Path(args.output_json)
        if not output_path.is_absolute():
            output_path = (repo_root / output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        matches_payload = []
        for item in matches:
            copy_item = dict(item)
            copy_item.pop("_a_idx", None)
            copy_item.pop("_b_idx", None)
            matches_payload.append(copy_item)

        payload = {
            "generated_at": now_iso(),
            "table": args.table,
            "topic_filter": args.topic or None,
            "rows_loaded": len(raw_rows),
            "rows_usable": len(rows),
            "rows_invalid_or_unparseable": skipped_invalid,
            "thresholds": {
                "min_shared_weight": args.min_shared_weight,
                "min_shared_hashes": args.min_shared_hashes,
                "min_weighted_jaccard": args.min_weighted_jaccard,
                "min_overlap": args.min_overlap,
                "max_simhash_hamming": args.max_simhash_hamming,
                "min_confidence": args.min_confidence,
                "cluster_min_confidence": args.cluster_min_confidence,
            },
            "stats": {
                "candidate_pairs": stats["candidate_pairs"],
                "pruned_pairs": stats["pruned_pairs"],
                "matched_pairs": len(matches_payload),
                "clusters": len(clusters),
                "cluster_rows_assigned": len(cluster_assignments),
                "cluster_rows_updated": cluster_rows_updated,
                "cluster_rows_cleared": cluster_rows_cleared,
            },
            "matches": matches_payload,
            "clusters": clusters,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{now_iso()}] Wrote JSON results: {output_path}")


if __name__ == "__main__":
    main()
