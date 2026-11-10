# jar2java
simple tool that implements vineflower to decompile java source code in a folder. This tool will decompile `.jar/.war/.ear/.class` file to `.java`. Useful for searching when audit source code

> the tool works **in place**: a jar/class file is replaced by its decompiled source once it is decompiled successfully - run it on a copy of the target.

options:
```
-f : folder contain jar file
-jp : (optional) specify java binary path if it's not available on your path env (jdk >= 17)
-t : (optional) how many decompilers run in parallel, default is automatic
```

examples:
```python
# decompile all files in folder
python3 main.py -f /path/to/folder/

# specify jdk path to use
python3 main.py -f /path/to/folder/ -jp /opt/jdk17/bin/java
```

## how it works
- every `.jar/.war/.ear` is decompiled to a folder with the same name next to it, nested jars (fat jar, `WEB-INF/lib`...) are decompiled too
- loose `.class` files are decompiled per classpath root (e.g. `WEB-INF/classes`), so inner classes are merged into their outer class like in the original source
- small jars run in parallel, big jars (> 20mb) run one at a time with all cpus and memory; memory and thread count are picked automatically from the machine
- progress line shows done/total, %, elapsed, eta and the % done inside the jar being decompiled
- a failed item (out of memory...) is retried alone with all cpus and memory
- a stuck item (vineflower silent for 10 minutes) is killed and retried with a 120s limit per method (jdk < 20 only), methods over the limit are left undecompiled and listed in `jar2java.log`
- if an item still fails the original file is kept and listed in the summary; running the tool again only processes what is left
- a big jar gives no source files until it is completely done (vineflower writes them at the end)
- corrupt archives and archives with unsafe entry path (zip slip) are skipped and reported
- minified `.xml` files are beautified
- summary at the end, details of failures are written to `jar2java.log` in the target folder (exit code is 1 when something failed)
- the newest `vineflower*.jar` next to `main.py` is used, the tool can be started from any directory
