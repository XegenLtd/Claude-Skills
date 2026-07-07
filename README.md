# Xegen Claude Code skills

A [Claude Code](https://code.claude.com) **plugin marketplace** — a catalog of Xegen's
skills and tooling that anyone can install with a couple of commands.

Marketplace name: **`xegen-skills`** · Repo: `XegenLtd/Claude-Skills`

## Install

In Claude Code:

```
/plugin marketplace add XegenLtd/Claude-Skills
/plugin install projectlayer-build@xegen-skills
```

The first command registers this marketplace (by its repo); the second installs a plugin
from it using `<plugin-name>@xegen-skills`.

Refresh later with `/plugin marketplace update xegen-skills`.

## Plugins

### `projectlayer-build`

Authors development plans from a [ProjectLayer.app](https://projectlayer.app) task's
description and builds the code from a plan, keeping the task's status in sync via the
ProjectLayer REST API.

**Setup:** set a **write-scoped** API token (ProjectLayer → Settings):

```bash
export PROJECTLAYER_API_TOKEN=pl_live_xxx
# optional, for self-hosted instances:
# export PROJECTLAYER_BASE_URL=https://your-domain/api/v1
```

Requires Python 3 (standard library only). The skill is model-invoked — just ask, e.g.
*"Write a plan for RSPH-23 and push it back"* or *"Build RSPH-14"*.

---

## Repository layout

```
Claude-Skills/                          ← this repo (the marketplace)
├── .claude-plugin/
│   └── marketplace.json                ← the catalog: lists every plugin
└── plugins/                            ← one folder per plugin (metadata.pluginRoot)
    └── projectlayer-build/
        ├── .claude-plugin/
        │   └── plugin.json             ← this plugin's manifest
        └── skills/
            └── projectlayer-build/
                ├── SKILL.md
                └── scripts/...
```

Only `plugin.json` / `marketplace.json` go inside a `.claude-plugin/` folder. Everything
else (`skills/`, `scripts/`, …) sits at the plugin root.

## Growing the marketplace

There are two ways to add capability. Pick based on how related the new thing is.

### Add a new skill to an existing plugin

Best when the skill is part of the same tool (e.g. another ProjectLayer capability). A
plugin auto-loads **every** skill under its `skills/` directory — no catalog edit needed.

```bash
mkdir -p plugins/projectlayer-build/skills/<new-skill>
$EDITOR plugins/projectlayer-build/skills/<new-skill>/SKILL.md
```

Bump the plugin's `version` in both `plugins/projectlayer-build/.claude-plugin/plugin.json`
and its entry in `marketplace.json` so users get the update.

### Add a whole new plugin

Best for a distinct, independently-installable tool. Each plugin is its own installable
unit under `plugins/`.

1. Scaffold it:
   ```bash
   mkdir -p plugins/<new-plugin>/.claude-plugin plugins/<new-plugin>/skills/<skill-name>
   ```
2. Create `plugins/<new-plugin>/.claude-plugin/plugin.json`:
   ```json
   {
     "name": "<new-plugin>",
     "description": "What it does and when to use it.",
     "version": "0.1.0",
     "author": { "name": "Xegen", "email": "jay@xegen.co.uk" }
   }
   ```
3. Add the skill(s) under `plugins/<new-plugin>/skills/<skill-name>/SKILL.md`.
4. Register it in `.claude-plugin/marketplace.json` by appending to the `plugins` array.
   Because `metadata.pluginRoot` is `./plugins`, `source` is just the folder name:
   ```json
   {
     "name": "<new-plugin>",
     "source": "<new-plugin>",
     "description": "…",
     "version": "0.1.0",
     "author": { "name": "Xegen" }
   }
   ```

Users then install it with `/plugin install <new-plugin>@xegen-skills`.

### Before you push

Validate locally (the community review pipeline runs the same check):

```bash
claude plugin validate .
```

Then commit and push. Users pick up changes with `/plugin marketplace update xegen-skills`;
they only receive a plugin update when its `version` changes.
