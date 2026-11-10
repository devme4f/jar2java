# jar2java — project direction

Instructions from the owner. Read before doing any work in this repo.

## What this tool is

A simple tool that decompiles Java bytecode (`.jar`, `.class`) to `.java` source,
used for **security audit / research** (the output gets grepped and read by auditor).

## Goals (in priority order)

1. **Simple** — it must just work with a single flag pointing at a folder (`-f <folder>`).
   Anything else must be optional with a sensible automatic default. No extra dependencies
   beyond Python stdlib + Java + the bundled Vineflower jar.
2. **Reliable** — handle errors (corrupt jars, timeouts, odd paths, read-only files, Ctrl+C)
   without crashing or hanging; never lose an input file that was not successfully decompiled;
   always finish with a clear summary of what failed.
3. **Faithful output** — decompiled source should match the original as closely as possible.
4. **Powerful & efficient** — fast on large codebases, shows progress (counts, %, ETA).
5. **Keeps using Vineflower** as the decompiler. Do not swap it for another one.

## Rules for Claude

- **Ask the owner's permission before committing anything** to the local repo. Every time.
- **Never push to the remote GitHub repo.** Not even when asked to "finish up" — the owner
  pushes themself.
- Keep the tool simple: prefer improving defaults over adding flags.
