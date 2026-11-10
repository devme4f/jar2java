import argparse, collections, glob, os, re, shutil, stat, struct, subprocess, sys, threading, time, zipfile
import xml.dom.minidom
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_EXTS = ('.jar', '.war', '.ear')
SOURCE_EXTS = ('.java', '.kt')
TMP_DIR_NAME = '.jar2java_tmp'
LOG_FILE_NAME = 'jar2java.log'
MAX_HEAP_MB = 16 * 1024
HEAVY_BYTES = 20_000_000    # bigger jars run alone, with all cpus and memory
TIMEOUT_SECONDS = 900       # hard limit for one item: 15 minutes ...
TIMEOUT_SECONDS_PER_MB = 60 # ... plus 1 minute per mb
STALL_SECONDS = 600         # vineflower silent for this long = stuck
METHOD_SECONDS = 120        # per-method limit, only used when retrying a stuck item

ACTIVE_PROCS = set()
ACTIVE_LOCK = threading.Lock()
STOPPING = threading.Event()


# ---------- environment ----------

def findVineflower():
    def version(path):
        return [int(n) for n in re.findall(r'\d+', os.path.basename(path))]
    jars = sorted(glob.glob(os.path.join(glob.escape(SCRIPT_DIR), 'vineflower*.jar')), key=version)
    if not jars:
        sys.exit(f'ERROR: no vineflower*.jar found next to this script ({SCRIPT_DIR})')
    return jars[-1]

def checkJavaPath(java_path):
    try:
        cmd = subprocess.run([java_path, '-version'], capture_output=True, text=True, timeout=60)
        out = cmd.stderr + cmd.stdout
        if cmd.returncode != 0:
            raise OSError(out.strip())
    except (OSError, subprocess.SubprocessError) as e:
        sys.exit(f"ERROR: java not usable ({e}), make sure it is available on your PATH or specify it with '-jp' flag")
    m = re.search(r'version "(\d+)', out)
    if m and int(m.group(1)) < 17:
        sys.exit(f'ERROR: java {m.group(1)} is too old, vineflower needs jdk >= 17 (use -jp to point to a newer one)')
    print(f'INFO: java: {out.splitlines()[0].strip() if out.strip() else java_path}')
    return int(m.group(1)) if m else 17

def totalRamMb():
    try:
        if os.name == 'nt':
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ('ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile',
                            'ullAvailPageFile', 'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys // (1024 * 1024)
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') // (1024 * 1024)
    except Exception:
        return 8 * 1024


# ---------- progress ----------

def fmtTime(seconds):
    seconds = int(seconds)
    h, m, s = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'

class Progress:
    def __init__(self, label):
        self.label = label
        self.total = self.done = self.failed = 0
        self.total_bytes = self.done_bytes = 0
        self.start = time.time()
        self.tty = sys.stdout.isatty()
        self.last_draw = 0
        self.running = {}

    def add(self, jobs):
        self.total += len(jobs)
        self.total_bytes += sum(j.size for j in jobs)

    def finish(self, job, ok):
        self.done += 1
        self.done_bytes += job.size
        self.failed += 0 if ok else 1

    def unfinish(self, job):
        # a failed job is going to be retried
        self.done -= 1
        self.done_bytes -= job.size
        self.failed -= 1

    def line(self):
        elapsed = time.time() - self.start
        running = sorted(self.running.values(), key=lambda j: -j.size)
        done_bytes = self.done_bytes + sum(j.size * j.fraction for j in running)
        pct = 100 * done_bytes / self.total_bytes if self.total_bytes else 100
        eta = fmtTime(elapsed * (self.total_bytes - done_bytes) / done_bytes) if done_bytes else '--:--'
        text = f'{self.label}: {self.done}/{self.total} {pct:3.0f}% | elapsed {fmtTime(elapsed)} | eta {eta}'
        if self.failed:
            text += f' | failed {self.failed}'
        if running:
            text += f' | now: {os.path.basename(running[0].path)} {100 * running[0].fraction:.0f}%'
            text += f' (+{len(running) - 1})' if len(running) > 1 else ''
        return text

    def draw(self, force=False):
        now = time.time()
        if self.tty:
            width = shutil.get_terminal_size((100, 20)).columns - 1
            sys.stdout.write('\r' + self.line()[:width].ljust(width))
            sys.stdout.flush()
        elif force or now - self.last_draw >= 10:
            print(self.line(), flush=True)
            self.last_draw = now

    def message(self, text):
        if self.tty:
            sys.stdout.write('\r' + ' ' * (shutil.get_terminal_size((100, 20)).columns - 1) + '\r')
        print(text, flush=True)

    def close(self):
        self.running.clear()
        self.draw(force=True)
        if self.tty:
            print()


# ---------- vineflower ----------

class Job:
    def __init__(self, kind, path, size):
        self.kind = kind            # 'jar' or 'classes'
        self.path = path            # archive file, or root folder of a class tree
        self.size = size
        self.class_files = []       # kind == 'classes' only
        self.error = None
        self.fatal = False          # True when a retry cannot help
        self.stalled = False        # last run was killed because vineflower went silent
        self.solo = False           # runs alone with all cpus and memory
        self.rescue = False         # runs with a per-method time limit
        self.history = []           # errors of earlier attempts
        self.units = 1              # top level classes, for progress inside the job
        self.fraction = 0           # progress inside the job while it runs, 0..1
        self.output = ''
        self.left = 0               # class files left undecompiled (kind == 'classes')

    def name(self):
        return os.path.basename(self.path) if self.kind == 'jar' else self.path

class Config:
    def __init__(self, java, java_major, vineflower, tmp_dir):
        self.java = java
        self.java_major = java_major
        self.vineflower = vineflower
        self.tmp_dir = tmp_dir

def stopAll():
    STOPPING.set()
    with ACTIVE_LOCK:
        for proc in ACTIVE_PROCS:
            proc.kill()

def removeFile(path):
    try:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)
    except OSError:
        pass

def removeTree(path):
    # java may hold a file for a moment after it was killed (windows), so try a few times
    def writable(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    for _ in range(10):
        try:
            shutil.rmtree(path, onerror=writable)
        except OSError:
            pass
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return False

def moveTree(src, dst):
    # move a folder into place, merging when the destination already exists
    if not os.path.exists(dst):
        try:
            os.rename(src, dst)
            return
        except OSError:
            pass
    for dirpath, _, files in os.walk(src):
        dest_dir = os.path.join(dst, os.path.relpath(dirpath, src))
        os.makedirs(dest_dir, exist_ok=True)
        for f in files:
            os.replace(os.path.join(dirpath, f), os.path.join(dest_dir, f))

def runVineflower(conf, job, source, dest, heap_mb, threads):
    # returns (error or None, vineflower console output); updates job.fraction / job.stalled while running
    command = [conf.java, f'-Xmx{heap_mb}m', '-jar', conf.vineflower, f'--thread-count={threads}']
    if job.rescue:
        command.append(f'--max-time-per-method={METHOD_SECONDS}')
    command += [source, dest]
    timeout = TIMEOUT_SECONDS + TIMEOUT_SECONDS_PER_MB * job.size / 1_000_000
    job.fraction, job.stalled = 0, False
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors='replace')
    except OSError as e:
        return f'cannot start java: {e}', ''
    with ACTIVE_LOCK:
        ACTIVE_PROCS.add(proc)

    start = last_output = time.time()
    killed = []
    def watchdog():
        while proc.poll() is None:
            now = time.time()
            if now - last_output > STALL_SECONDS:
                job.stalled = True
                killed.append(f'stuck, no progress for {fmtTime(STALL_SECONDS)} at {100 * job.fraction:.0f}%')
            elif now - start > timeout:
                killed.append(f'timeout after {fmtTime(timeout)} at {100 * job.fraction:.0f}%')
            if killed:
                proc.kill()
                return
            time.sleep(1)
    threading.Thread(target=watchdog, daemon=True).start()

    # the INFO log is only used to follow progress; warnings, errors and stack traces are kept
    kept, tail, started = [], collections.deque(maxlen=60), 0
    try:
        for line in proc.stdout:
            last_output = time.time()
            if line.startswith('INFO:'):
                tail.append(line)
                if 'Preprocessing class' in line:
                    started += 1
                    job.fraction = min(0.99, started / job.units)
            elif len(kept) < 20000:
                kept.append(line)
        proc.wait()
    finally:
        with ACTIVE_LOCK:
            ACTIVE_PROCS.discard(proc)
    out = ''.join(kept)
    if STOPPING.is_set():
        return 'interrupted', out
    if killed:
        # the last classes vineflower started are the suspects
        return killed[0], out + ''.join(tail)
    if proc.returncode != 0:
        reason = 'out of memory' if 'OutOfMemoryError' in out else f'vineflower exit code {proc.returncode}'
        return reason, out + ''.join(tail)
    return None, out

def hasSource(folder):
    for _, _, files in os.walk(folder):
        if any(f.endswith(SOURCE_EXTS) for f in files):
            return True
    return False

def inspectArchive(path):
    # returns (error or None, number of top level classes) - cheap check before starting a JVM
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        return f'not a valid archive ({e})', 0
    for n in names:
        if n.startswith(('/', '\\')) or '..' in re.split(r'[\\/]', n) or re.match(r'^[A-Za-z]:', n):
            return f'unsafe entry path in archive (zip slip): {n!r}', 0
    return None, sum(1 for n in names if n.endswith('.class') and '$' not in n.rsplit('/', 1)[-1])

def decompileJar(conf, job, heap_mb, threads):
    job.error, class_count = inspectArchive(job.path)
    if job.error:
        job.fatal = True
        return job
    job.units = max(1, class_count)
    # vineflower writes into a temporary folder which only replaces the jar when everything went fine,
    # a failed / stuck / interrupted jar leaves nothing behind
    work = os.path.join(conf.tmp_dir, f'{threading.get_ident()}_{time.time_ns()}')
    tmp_out = os.path.join(work, 'out')
    try:
        os.makedirs(tmp_out)
        job.error, job.output = runVineflower(conf, job, job.path, tmp_out, heap_mb, threads)
        if job.error is None and class_count and not hasSource(tmp_out):
            job.error = 'vineflower produced no source files'
        if job.error is None:
            moveTree(tmp_out, os.path.splitext(job.path)[0])
    except OSError as e:
        job.error = job.error or f'{type(e).__name__}: {e}'
    finally:
        removeTree(work)
    if job.error is None:
        removeFile(job.path)
    return job

def decompileClassTree(conf, job, heap_mb, threads):
    # loose .class files are packed into a temporary jar so that vineflower sees the whole tree at once
    # (inner classes get merged into their outer class) and never touches anything else in the folder
    work = os.path.join(conf.tmp_dir, f'{threading.get_ident()}_{time.time_ns()}')
    tmp_jar, tmp_out = os.path.join(work, 'classes.jar'), os.path.join(work, 'out')
    job.units = max(1, sum(1 for f in job.class_files if '$' not in os.path.basename(f)))
    try:
        os.makedirs(tmp_out)
        with zipfile.ZipFile(tmp_jar, 'w', zipfile.ZIP_STORED) as z:
            for f in job.class_files:
                z.write(f, os.path.relpath(f, job.path))
        job.error, job.output = runVineflower(conf, job, tmp_jar, tmp_out, heap_mb, threads)
        for dirpath, _, files in os.walk(tmp_out):
            for f in files:
                if f.endswith(SOURCE_EXTS):
                    dest_dir = os.path.join(job.path, os.path.relpath(dirpath, tmp_out))
                    os.makedirs(dest_dir, exist_ok=True)
                    os.replace(os.path.join(dirpath, f), os.path.join(dest_dir, f))
    except OSError as e:
        job.error = job.error or f'{type(e).__name__}: {e}'
    finally:
        removeTree(work)

    left = []
    for f in job.class_files:
        folder, stem = os.path.split(f[:-len('.class')])
        outer = stem.split('$')[0] or stem
        if any(os.path.exists(os.path.join(folder, n + ext)) for n in {stem, outer} for ext in SOURCE_EXTS):
            removeFile(f)
        else:
            left.append(f)
    job.class_files, job.left = left, len(left)
    if left and job.error is None:
        job.error = f'{len(left)} class files were not decompiled'
    return job

def runJobs(conf, jobs, progress, workers, heap_total_mb):
    # run jobs in parallel, biggest first; returns the list of failed jobs
    if not jobs:
        return []
    cpus = os.cpu_count() or 4
    workers = max(1, min(workers, len(jobs)))
    threads = max(1, cpus // workers)
    heap_mb = int(max(1024, min(MAX_HEAP_MB, heap_total_mb / workers)))
    failed = []

    def run(job):
        if STOPPING.is_set():
            job.error = 'interrupted'
            return job
        progress.running[id(job)] = job
        func = decompileJar if job.kind == 'jar' else decompileClassTree
        try:
            return func(conf, job, heap_mb, threads)
        except Exception as e:  # never let one job take the whole run down
            job.error = f'{type(e).__name__}: {e}'
            return job
        finally:
            progress.running.pop(id(job), None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(run, j) for j in sorted(jobs, key=lambda j: -j.size)}
        try:
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    job = fut.result()
                    progress.finish(job, job.error is None)
                    if job.error:
                        failed.append(job)
                        if not STOPPING.is_set():
                            progress.message(f'FAILED: {job.name()}: {job.error}')
                    elif job.history:
                        progress.message(f'RECOVERED: {job.name()}')
                progress.draw()
        except KeyboardInterrupt:
            # kill the running decompilers, drop the queued ones, wait for the workers to clean up
            stopAll()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
    return failed

def planRetry(conf, job):
    # returns a description of the next attempt, or None when another attempt cannot help
    if job.fatal or job.error == 'interrupted':
        return None
    if job.stalled and not job.rescue and conf.java_major < 20:
        # vineflower's per-method limit relies on Thread.stop(), gone in java 20+
        job.solo = job.rescue = True
        return f'alone, giving up on any method that takes more than {METHOD_SECONDS}s'
    if not job.solo:
        job.solo = True
        return 'alone with all cpus and memory'
    return None

def runAll(conf, jobs, progress, workers, heap_total_mb):
    # small jobs in parallel, then big jobs and retries one at a time; returns the jobs that failed for good
    solo = [j for j in jobs if j.size >= HEAVY_BYTES]
    for job in solo:
        job.solo = True
    if solo and len(solo) < len(jobs):
        progress.message(f'STATUS: {len(solo)} big item(s) will run alone with all cpus after the others')
    failed = runJobs(conf, [j for j in jobs if not j.solo], progress, workers, heap_total_mb)
    final_failed = []
    while True:
        for job in failed:
            plan = None if STOPPING.is_set() else planRetry(conf, job)
            if plan:
                progress.unfinish(job)
                progress.message(f'RETRY: {job.name()}: {plan}')
                job.history.append(job.error)
                solo.append(job)
            else:
                job.error = '; retry: '.join(job.history + [job.error])
                final_failed.append(job)
        if not solo or STOPPING.is_set():
            return final_failed
        failed, solo = runJobs(conf, solo, progress, 1, heap_total_mb), []


# ---------- scanning ----------

def walk(folder):
    for dirpath, dirnames, filenames in os.walk(folder):
        if TMP_DIR_NAME in dirnames:
            dirnames.remove(TMP_DIR_NAME)
        yield dirpath, filenames

def findArchives(folder, skip):
    jobs = []
    for dirpath, filenames in walk(folder):
        for f in filenames:
            path = os.path.join(dirpath, f)
            if f.lower().endswith(ARCHIVE_EXTS) and path not in skip:
                try:
                    jobs.append(Job('jar', path, os.path.getsize(path)))
                except OSError:
                    pass
    return jobs

def classPackage(class_file):
    # package of a compiled class as a relative folder ('com/acme'), read from the constant pool
    try:
        with open(class_file, 'rb') as f:
            data = f.read()
        if data[:4] != b'\xca\xfe\xba\xbe':
            return None
        count = struct.unpack_from('>H', data, 8)[0]
        pos, i, utf8, classes = 10, 1, {}, {}
        while i < count:
            tag = data[pos]
            if tag == 1:
                length = struct.unpack_from('>H', data, pos + 1)[0]
                utf8[i] = data[pos + 3:pos + 3 + length]
                pos += 3 + length
            elif tag in (7, 8, 16, 19, 20):
                if tag == 7:
                    classes[i] = struct.unpack_from('>H', data, pos + 1)[0]
                pos += 3
            elif tag == 15:
                pos += 4
            elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
                pos += 5
            elif tag in (5, 6):
                pos += 9
                i += 1
            else:
                return None
            i += 1
        this_class = struct.unpack_from('>H', data, pos + 2)[0]
        name = utf8[classes[this_class]].decode('utf-8', 'replace')
        return os.path.dirname(name)
    except (OSError, IndexError, KeyError, struct.error):
        return None

def findClassTrees(folder):
    # group loose .class files by classpath root (e.g. WEB-INF/classes), one job per root
    roots = {}
    for dirpath, filenames in walk(folder):
        class_files = [os.path.join(dirpath, f) for f in filenames if f.endswith('.class')]
        if not class_files:
            continue
        root = dirpath
        package = classPackage(class_files[0])
        if package:
            parts = package.split('/')
            dir_parts = os.path.normpath(dirpath).split(os.sep)
            if dir_parts[-len(parts):] == parts:
                root = os.sep.join(dir_parts[:-len(parts)])
                if not root or root.endswith(':'):
                    root += os.sep
        job = roots.setdefault(root, Job('classes', root, 0))
        for f in class_files:
            try:
                job.size += os.path.getsize(f)
                job.class_files.append(f)
            except OSError:
                pass
    return list(roots.values())


# ---------- xml ----------

# beautify xml file, resource file in jar after decompile is minified
def beautifyXML(folder):
    count = errors = 0
    for dirpath, filenames in walk(folder):
        for f in filenames:
            if not f.lower().endswith('.xml') or STOPPING.is_set():
                continue
            xml_file = os.path.join(dirpath, f)
            try:
                with open(xml_file, 'rb') as fh:
                    data = fh.read()
                if not data.strip() or data.count(b'\n') >= 3:
                    continue
                parsed_xml = xml.dom.minidom.parseString(data)
                new_xml = parsed_xml.toprettyxml(indent='\t', encoding=parsed_xml.encoding or 'utf-8')
                with open(xml_file, 'wb') as fh:
                    fh.write(new_xml)
                count += 1
            except Exception:
                errors += 1
    print(f'STATUS: beautified {count} minified xml files' + (f' ({errors} skipped, not well-formed)' if errors else ''))


# ---------- main ----------

def writeLog(folder, failed, warnings, summary):
    log_file = os.path.join(folder, LOG_FILE_NAME)
    try:
        with open(log_file, 'w', encoding='utf-8', errors='replace') as f:
            f.write('\n'.join(summary) + '\n')
            if failed:
                f.write('\n===== FAILED =====\n')
                for job in failed:
                    f.write(f'\n--- {job.path}\n    {job.error}\n')
                    for c in job.class_files[:200]:
                        f.write(f'    left: {c}\n')
                    tail = job.output.strip().splitlines()[-100:]
                    f.write(''.join(f'    | {line}\n' for line in tail))
            if warnings:
                f.write('\n===== METHODS/CLASSES VINEFLOWER COULD NOT DECOMPILE (see "$VF:" comments in the source) =====\n')
                f.write('\n'.join(warnings) + '\n')
        return log_file
    except OSError as e:
        print(f'ERROR: cannot write log file: {e}')
        return None

def main():
    parser = argparse.ArgumentParser(description='decompile every .jar/.war/.ear/.class in a folder to .java (in place)')
    parser.add_argument('-f', '--folder', required=True, help='folder contain jar/class files')
    parser.add_argument('-jp', '--java-path', default='java', help='specify java binary path (jdk >= 17)')
    parser.add_argument('-t', '--thread', type=int, help='how many decompilers run in parallel, default is automatic')
    args = parser.parse_args()

    folder = os.path.abspath(args.folder.strip().strip('"'))
    if not os.path.isdir(folder):
        sys.exit(f'ERROR: folder not found: {folder}')
    print(f'INFO: Location: {folder}')

    java_major = checkJavaPath(args.java_path)
    conf = Config(args.java_path, java_major, findVineflower(), os.path.join(folder, TMP_DIR_NAME))
    cpus = os.cpu_count() or 4
    workers = args.thread if args.thread and args.thread > 0 else max(1, min(8, cpus // 4))
    heap_total_mb = totalRamMb() * 0.7
    print(f'INFO: {os.path.basename(conf.vineflower)}, {cpus} cpus, up to {workers} parallel decompilers')

    if os.path.exists(conf.tmp_dir) and not removeTree(conf.tmp_dir):     # left by a run that was killed hard
        sys.exit(f'ERROR: cannot remove old temporary folder: {conf.tmp_dir}')

    start = time.time()
    failed, warnings = [], []
    done_jars = done_trees = 0
    try:
        # jars: repeat until no new (nested) jar shows up - fat jars / wars contain more jars
        progress = Progress('jars')
        seen, round_no = set(), 0
        while not STOPPING.is_set():
            jobs = findArchives(folder, seen)
            if not jobs:
                break
            round_no += 1
            seen.update(j.path for j in jobs)
            progress.add(jobs)
            progress.message(f'STATUS: found {len(jobs)} ' + ('jar files' if round_no == 1 else 'nested jar files'))
            round_failed = runAll(conf, jobs, progress, workers, heap_total_mb)
            failed += round_failed
            done_jars += len(jobs) - len(round_failed)
            warnings += [f'{j.path}: {l.strip()}' for j in jobs for l in j.output.splitlines() if "couldn't be decompiled" in l]
        if progress.total:
            progress.close()

        if not STOPPING.is_set():
            jobs = findClassTrees(folder)
            if jobs:
                progress = Progress('class folders')
                progress.add(jobs)
                total_classes = sum(len(j.class_files) for j in jobs)
                progress.message(f'STATUS: found {total_classes} loose class files in {len(jobs)} class folders')
                tree_failed = runAll(conf, jobs, progress, workers, heap_total_mb)
                failed += tree_failed
                done_trees = len(jobs) - len(tree_failed)
                warnings += [f'{j.path}: {l.strip()}' for j in jobs for l in j.output.splitlines() if "couldn't be decompiled" in l]
                progress.close()

        if not STOPPING.is_set():
            beautifyXML(folder)
    except KeyboardInterrupt:
        stopAll()
        print('\nINTERRUPTED: stopping, files not decompiled yet are left untouched')
    finally:
        if os.path.exists(conf.tmp_dir) and not removeTree(conf.tmp_dir):
            print(f'WARNING: could not remove temporary folder: {conf.tmp_dir}')

    failed = [j for j in failed if j.error != 'interrupted']
    summary = [f'jar2java summary for {folder}',
               f'time: {fmtTime(time.time() - start)}',
               f'jars decompiled: {done_jars}',
               f'class folders decompiled: {done_trees}',
               f'failed: {len(failed)}',
               f'methods/classes vineflower could not decompile: {len(warnings)}']
    print('\n===== SUMMARY =====')
    print('\n'.join(summary[1:]))
    for job in failed:
        print(f'FAILED: {job.path}: {job.error}')
    if failed or warnings:
        log_file = writeLog(folder, failed, warnings, summary)
        if log_file:
            print(f'details: {log_file}')
    if STOPPING.is_set():
        sys.exit(130)
    sys.exit(1 if failed else 0)

if __name__ == '__main__':
    main()
