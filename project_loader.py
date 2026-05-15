"""扫描 projects/*.json 加载并校验配置，同步到 DB。

校验规则（plan §项目配置文件格式）：
- 必填字段：slug / name / description / trial_url / max_feedback_count / invite_tokens
- slug 必须是 URL-safe（仅 [a-zA-Z0-9_-]，避免路径穿越、查询串污染）
- name / description 必须非空字符串
- trial_url 必须以 http(s):// 开头（防止 javascript:、file:、data: 等危险 scheme
  被注入到项目卡页的"开始试用"超链接）
- max_feedback_count 必须是整数（拒绝 bool，因 bool 是 int 子类）且 1..100
- custom_questions 数组长度 <= 2，元素为非空字符串
- invite_tokens 非空字符串数组；同一 token 不可被多个项目声明
- 部署期任一校验失败 → 拒绝启动（抛 ProjectConfigError）
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import db

log = logging.getLogger("probe.loader")

REQUIRED_FIELDS = ("slug", "name", "description", "trial_url",
                   "max_feedback_count", "invite_tokens")

SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
SAFE_URL_SCHEMES = ("http://", "https://")


class ProjectConfigError(Exception):
    pass


def _require_nonempty_str(cfg: dict, key: str, source: str) -> None:
    val = cfg[key]
    if not isinstance(val, str) or not val.strip():
        raise ProjectConfigError(f"{source}: {key} must be non-empty string, got {val!r}")


def _validate(cfg: dict, source: str) -> None:
    for field in REQUIRED_FIELDS:
        if field not in cfg:
            raise ProjectConfigError(f"{source}: missing field '{field}'")

    # slug：URL-safe 才能直接拼到 /p/<slug>，避免路径穿越和查询串污染
    if not isinstance(cfg["slug"], str) or not SLUG_RE.match(cfg["slug"]):
        raise ProjectConfigError(
            f"{source}: slug must match {SLUG_RE.pattern}, got {cfg['slug']!r}"
        )

    _require_nonempty_str(cfg, "name", source)
    _require_nonempty_str(cfg, "description", source)
    _require_nonempty_str(cfg, "trial_url", source)

    # trial_url 必须是 http(s) 才能渲染到项目卡的 href 里：
    # 否则 "javascript:alert(...)" / "data:..." 这种 scheme 会直接被浏览器执行。
    trial_url = cfg["trial_url"].strip()
    if not trial_url.lower().startswith(SAFE_URL_SCHEMES):
        raise ProjectConfigError(
            f"{source}: trial_url must start with http:// or https://, "
            f"got {trial_url!r}"
        )

    # bool 是 int 的子类，必须显式排除：True/False 不应作 max_feedback_count
    max_n = cfg["max_feedback_count"]
    if isinstance(max_n, bool) or not isinstance(max_n, int):
        raise ProjectConfigError(
            f"{source}: max_feedback_count must be int, got {type(max_n).__name__}={max_n!r}"
        )
    if max_n <= 0 or max_n > 100:
        raise ProjectConfigError(
            f"{source}: max_feedback_count must be in 1..100, got {max_n}"
        )

    custom = cfg.get("custom_questions", []) or []
    if not isinstance(custom, list):
        raise ProjectConfigError(f"{source}: custom_questions must be list")
    if len(custom) > 2:
        raise ProjectConfigError(
            f"{source}: custom_questions limit is 2, got {len(custom)}"
        )
    for i, q in enumerate(custom):
        if not isinstance(q, str) or not q.strip():
            raise ProjectConfigError(
                f"{source}: custom_questions[{i}] must be non-empty string, got {q!r}"
            )

    tokens = cfg["invite_tokens"]
    if not isinstance(tokens, list) or not tokens:
        raise ProjectConfigError(f"{source}: invite_tokens must be non-empty list")
    for t in tokens:
        if not isinstance(t, str) or not t.strip():
            raise ProjectConfigError(f"{source}: invite token must be non-empty string")

    # single_use_tokens 必须严格 bool，避免"false"字符串被当真
    sut = cfg.get("single_use_tokens", False)
    if not isinstance(sut, bool):
        raise ProjectConfigError(
            f"{source}: single_use_tokens must be true/false, got {sut!r}"
        )


def load_all(projects_dir: Path) -> int:
    """扫描目录下所有 *.json 并 upsert。返回加载条数。"""
    if not projects_dir.exists():
        log.warning("projects dir not found: %s", projects_dir)
        return 0

    # 跨项目去重：同一 invite_token 不能在两个配置中出现，否则后者会通过
    # ON CONFLICT(token) 静默改写 project_slug，把别人的 token 转给自己用。
    seen_tokens: dict[str, str] = {}
    count = 0

    for path in sorted(projects_dir.glob("*.json")):
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProjectConfigError(f"{path}: invalid json: {exc}") from exc

        _validate(cfg, str(path))

        for tok in cfg["invite_tokens"]:
            if tok in seen_tokens and seen_tokens[tok] != cfg["slug"]:
                raise ProjectConfigError(
                    f"{path}: invite_token {tok!r} already declared by project "
                    f"{seen_tokens[tok]!r}; tokens must be unique across projects"
                )
            seen_tokens[tok] = cfg["slug"]

        custom_json = (
            json.dumps(cfg["custom_questions"], ensure_ascii=False)
            if cfg.get("custom_questions") else None
        )

        db.upsert_project(
            slug=cfg["slug"],
            name=cfg["name"],
            description=cfg["description"],
            trial_url=cfg["trial_url"].strip(),
            max_feedback_count=cfg["max_feedback_count"],
            custom_questions_json=custom_json,
        )

        is_single = 1 if cfg.get("single_use_tokens", False) else 0
        for token in cfg["invite_tokens"]:
            db.upsert_invite_token(token=token,
                                    project_slug=cfg["slug"],
                                    is_single_use=is_single)
        count += 1
        log.info("loaded project: %s (%d tokens, single_use=%s)",
                 cfg["slug"], len(cfg["invite_tokens"]), bool(is_single))

    return count
