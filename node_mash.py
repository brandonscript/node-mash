"""Dependency cleanup. No writes unless --apply is supplied."""
import argparse
import ctypes
import contextlib
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid

JUNK = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
SKIP = {'.git', '.hg', '.svn', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}


def dependency(path):
    return path.name in {'node_modules', '__pycache__'} or (path.name in {'.venv', 'venv', 'env'} and (path / 'pyvenv.cfg').is_file())


def discover(roots, store, report_dir=None):
    found = set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f'Root must be a real directory: {root}')
        for base, dirs, _ in os.walk(root, onerror=raise_error):
            path = Path(base)
            if dependency(path):
                found.add(path)
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if (d not in SKIP or d == '__pycache__') and not (path / d).is_symlink() and (path / d) not in {store, report_dir}]
    return sorted(found)


def raise_error(error):
    raise error


def missing_event(path, stage):
    print(json.dumps({'event': 'skipped', 'path': str(path),
                      'reason': 'disappeared before replacement', 'stage': stage}),
          file=sys.stderr, flush=True)


def scan_error(error):
    if isinstance(error, FileNotFoundError):
        missing_event(error.filename, 'scan')
    else:
        raise error


def activity(project, store, report_dir=None):
    latest = 0.0
    for base, dirs, files in os.walk(project, onerror=raise_error):
        path = Path(base)
        dirs[:] = [d for d in dirs if d not in SKIP and not (path / d).is_symlink() and not dependency(path / d) and (path / d) not in {store, report_dir}]
        for name in files:
            file = path / name
            if name not in JUNK and not file.is_symlink():
                latest = max(latest, file.stat().st_mtime)
    return latest or None  # No source evidence: never assume stale.


def fingerprint(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError(f'File changed during scan: {path}')
    return digest.hexdigest(), stat.S_IMODE(after.st_mode), after.st_size


def xattr_names(libc, path):
    listing = libc.listxattr
    listing.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    listing.restype = ctypes.c_ssize_t
    size = listing(os.fsencode(path), None, 0, 1)  # XATTR_NOFOLLOW
    if size < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(path))
    buffer = ctypes.create_string_buffer(size)
    count = listing(os.fsencode(path), buffer, size, 1)
    if count < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(path))
    return buffer.raw[:count].split(b'\0')[:-1]


def metadata_snapshot(path):
    """Capture stable metadata; exclude inode, ctime and access time by design."""
    info = path.stat()
    result = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid,
              'gid': info.st_gid, 'mtime_ns': info.st_mtime_ns,
              'flags': getattr(info, 'st_flags', 0)}
    if sys.platform == 'darwin':
        libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        getter = libc.getxattr
        getter.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        getter.restype = ctypes.c_ssize_t
        attrs = {}
        for name in sorted(xattr_names(libc, path)):
            size = getter(os.fsencode(path), name, None, 0, 0, 1)
            if size < 0:
                raise OSError(ctypes.get_errno(), 'Cannot read extended attribute', str(path))
            buffer = ctypes.create_string_buffer(size)
            count = getter(os.fsencode(path), name, buffer, size, 0, 1)
            if count < 0:
                raise OSError(ctypes.get_errno(), 'Cannot read extended attribute', str(path))
            attrs[name] = buffer.raw[:count]
        result['xattrs'] = attrs
        acl_get = libc.acl_get_file
        acl_get.argtypes = [ctypes.c_char_p, ctypes.c_int]
        acl_get.restype = ctypes.c_void_p
        acl_text = libc.acl_to_text
        acl_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        acl_text.restype = ctypes.c_void_p
        free = libc.acl_free
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_int
        acl = acl_get(os.fsencode(path), 0x100)  # ACL_TYPE_EXTENDED, sys/acl.h
        if not acl:
            if ctypes.get_errno() == errno.ENOENT and path.is_file():
                result['acl'] = b''  # macOS reports ENOENT when no ACL exists.
                return result
            raise OSError(ctypes.get_errno(), 'Cannot read ACL', str(path))
        text = None
        try:
            length = ctypes.c_ssize_t()
            text = acl_text(acl, ctypes.byref(length))
            if not text:
                raise OSError(ctypes.get_errno(), 'Cannot serialize ACL', str(path))
            result['acl'] = ctypes.string_at(text, length.value)
        finally:
            if text:
                free(text)
            free(acl)
    return result


def target_metadata_matches(actual, expected):
    """Compare metadata strictly except macOS-managed provenance values."""
    def normalize(value):
        value = dict(value)
        value['xattrs'] = {k: v for k, v in value.get('xattrs', {}).items()
                          if k != b'com.apple.provenance'}
        return value
    return normalize(actual) == normalize(expected)


def verify_clone(source, target, expected_content, expected_target_metadata,
                 expected_source_metadata):
    """Check the final path AFTER replacement; a failure terminates the run."""
    try:
        if target.is_symlink() or not stat.S_ISREG(target.lstat().st_mode):
            raise RuntimeError('replacement is not a regular file')
        if source.samefile(target) or target.stat().st_nlink != 1:
            raise RuntimeError('replacement is not independent')
        if fingerprint(target) != expected_content or fingerprint(source) != expected_content:
            raise RuntimeError('content mismatch')
        if not target_metadata_matches(metadata_snapshot(target), expected_target_metadata):
            raise RuntimeError('target metadata mismatch')
        if metadata_snapshot(source) != expected_source_metadata:
            raise RuntimeError('source metadata mismatch')
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f'POST-REPLACEMENT VERIFICATION FAILED: {target}: {error}; aborting remaining work') from error


def clone_replace(source, target):
    """Clone data, restore target metadata, verify, then atomically replace.

    clonefile failure never falls back to a copy or shared mutable link.
    macOS copyfile metadata flags come from Apple's copyfile.h.
    """
    if sys.platform != 'darwin':
        raise OSError(errno.ENOTSUP, 'APFS clone mode requires macOS')
    expected_content = fingerprint(target)
    if fingerprint(source) != expected_content:
        raise RuntimeError('Clone source and target differ')
    expected_target_metadata = metadata_snapshot(target)
    expected_source_metadata = metadata_snapshot(source)
    original = target.stat()
    source_state = source.stat()
    if source.is_symlink() or target.is_symlink():
        raise RuntimeError('Symlink encountered before cloning')
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    clone = libc.clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    metadata = libc.copyfile
    metadata.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    metadata.restype = ctypes.c_int
    temporary = target.with_name('.node-mash-' + uuid.uuid4().hex)
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    try:
        # CLONE_NOFOLLOW: clone a last-component symlink itself; verification
        # below rejects it if a concurrent change swaps the source.
        if clone(os.fsencode(source), os.fsencode(temporary), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
        if temporary.is_symlink():
            raise RuntimeError('Source changed to a symlink during cloning')
        # Remove source xattrs before restoring target xattrs and ACL/stat data.
        remove = libc.removexattr
        remove.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        remove.restype = ctypes.c_int
        for name in xattr_names(libc, temporary):
            if remove(os.fsencode(temporary), name, 1) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(temporary))
        flags = 7 | (1 << 18) | (1 << 19)  # METADATA | NOFOLLOW
        if metadata(os.fsencode(target), os.fsencode(temporary), None, flags) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
        # macOS can attach provenance to newly created files. Remove any
        # clone-only attribute names after metadata copying, then fail closed
        # if the target's attribute names still cannot be preserved.
        target_names = set(xattr_names(libc, target))
        for name in set(xattr_names(libc, temporary)) - target_names:
            if remove(os.fsencode(temporary), name, 1) != 0:
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(temporary))
        actual_names = set(xattr_names(libc, temporary))
        if (actual_names - {b'com.apple.provenance'}) != (target_names - {b'com.apple.provenance'}):
            raise RuntimeError(f'Clone extended attribute verification failed before replacement: {target}; source={source}; expected_names={sorted(os.fsdecode(n) for n in target_names)}; actual_names={sorted(os.fsdecode(n) for n in xattr_names(libc, temporary))}; original preserved')
        if not target_metadata_matches(metadata_snapshot(temporary), expected_target_metadata):
            raise RuntimeError(f'Clone metadata verification failed before replacement: {target}; original preserved')
        if fingerprint(temporary) != fingerprint(target):
            raise RuntimeError('Clone verification failed')
        copied = temporary.stat()
        if (copied.st_uid, copied.st_gid, copied.st_mtime_ns) != (original.st_uid, original.st_gid, original.st_mtime_ns):
            raise RuntimeError('Clone metadata verification failed')
        if identity(target.stat()) != identity(original) or identity(source.stat()) != identity(source_state):
            raise RuntimeError('File changed during cloning; original preserved')
        os.replace(temporary, target)
        verify_clone(source, target, expected_content, expected_target_metadata, expected_source_metadata)
        before_provenance = expected_target_metadata.get('xattrs', {}).get(b'com.apple.provenance')
        after_provenance = metadata_snapshot(target).get('xattrs', {}).get(b'com.apple.provenance')
        if before_provenance != after_provenance:
            print(json.dumps({'event': 'metadata_note', 'path': str(target),
                              'allowed_difference': 'com.apple.provenance',
                              'change': 'added' if before_provenance is None else 'removed' if after_provenance is None else 'value_changed'}), file=sys.stderr, flush=True)
    finally:
        temporary.unlink(missing_ok=True)


def replace_link(source, target, method):
    expected_content = fingerprint(source)
    temporary = target.with_name('.node-mash-' + uuid.uuid4().hex)
    try:
        if method == 'hardlink':
            os.link(source, temporary)
        else:
            temporary.symlink_to(source)
        os.replace(temporary, target)
        try:
            if fingerprint(target) != expected_content or fingerprint(source) != expected_content:
                raise RuntimeError('content mismatch')
            if method == 'hardlink' and (target.is_symlink() or not source.samefile(target)):
                raise RuntimeError('hardlink topology mismatch')
            if method == 'symlink' and (not target.is_symlink() or target.resolve() != source.resolve()):
                raise RuntimeError('symlink topology mismatch')
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f'POST-REPLACEMENT VERIFICATION FAILED: {target}: {error}; aborting remaining work') from error
    finally:
        temporary.unlink(missing_ok=True)


def _run(args):
    store = Path(args.store).expanduser().absolute()
    roots = [Path(p).expanduser().absolute() for p in args.roots]
    if any(root == store or store in root.parents for root in roots):
        raise ValueError('Scan roots must not be inside the content store')
    dependencies = discover(roots, store, getattr(args, "_report_dir", None))
    results = []
    now = time.time()
    stale_roots = []
    purge_bytes = 0
    dedupe_bytes = 0
    retained_dedupe_bytes = 0
    if args.command in {'purge', 'service'}:
        for dep in dependencies:
            if dep.name == '__pycache__':
                continue  # Cache directories are dedupe-only targets.
            newest = activity(dep.parent, store, getattr(args, "_report_dir", None))
            stale = newest is not None and now - newest > args.days * 86400
            if stale:
                stale_roots.append(dep)
                size = 0
                for base, dirs, files in os.walk(dep, onerror=raise_error):
                    dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                    for name in files:
                        file = Path(base) / name
                        info = file.lstat()
                        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            size += info.st_size
                purge_bytes += size
                item = {'estimated_bytes': size, 'action': 'purge' , 'path': str(dep), 'project_modified': newest, 'applied': False}
                if args.apply and (args.command != 'service' or args.purge):
                    # Recheck source activity immediately before deletion.
                    current = activity(dep.parent, store, getattr(args, "_report_dir", None))
                    if current is not None and now - current > args.days * 86400 and not dep.is_symlink():
                        shutil.rmtree(dep)
                        if os.path.lexists(dep):
                            raise RuntimeError(f'Purge verification failed: {dep}; aborting remaining work')
                        item['verified'] = True
                        item['applied'] = True
                    if item['applied']:
                        print(json.dumps({'event': 'verified', 'path': str(dep), 'action': 'purge'}), file=sys.stderr, flush=True)
                results.append(item)
    if args.command in {'dedupe', 'service'}:
        groups = {}
        scanned_stats = {}
        for dep in dependencies:
            if not dep.exists():
                continue
            for base, dirs, files in os.walk(dep, onerror=scan_error):
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                for name in files:
                    file = Path(base) / name
                    try:
                        info = file.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_size < args.min_bytes:
                            continue
                        key = fingerprint(file)
                    except FileNotFoundError:
                        missing_event(file, 'hash')
                        continue
                    scanned_stats[file] = info
                    groups.setdefault(key, []).append(file)
        for key, files in groups.items():
            if len(files) < 2:
                continue
            canonical = files[0]
            def estimate(candidates):
                if not candidates:
                    return 0
                source = candidates[0]
                eligible = [f for f in candidates[1:] if scanned_stats[f].st_nlink == 1 and (args.method not in {'hardlink', 'clone'} or scanned_stats[f].st_dev == scanned_stats[source].st_dev)]
                saving = len(eligible) * key[2]
                if args.method == 'symlink':
                    stored = store / f'{key[0]}-{key[1]:o}'
                    if not stored.exists():
                        saving = max(0, saving - key[2])
                return saving
            dedupe_bytes += estimate(files)
            retained_dedupe_bytes += estimate([f for f in files if not any(dep in f.parents for dep in stale_roots)])
            for target in files[1:]:
                try:
                    if canonical.samefile(target):
                        continue
                except FileNotFoundError:
                    missing_event(target, 'preflight')
                    continue
                item = {'method': args.method, 'action': 'dedupe', 'path': str(target), 'source': str(canonical), 'bytes': key[2], 'applied': False}
                if args.apply:
                    try:
                        if fingerprint(canonical) != key or fingerprint(target) != key:
                            raise RuntimeError(f'Files changed before replacement: {target}; source={canonical}; aborting dedupe')
                    except FileNotFoundError:
                        missing_event(target, 'preflight')
                        item['skipped'] = 'source or target disappeared before replacement'
                        results.append(item)
                        continue
                    if args.method == 'symlink':
                        store.mkdir(parents=True, exist_ok=True)
                        stored = store / f'{key[0]}-{key[1]:o}'
                        if not stored.exists():
                            temp = store / uuid.uuid4().hex
                            try:
                                shutil.copy2(canonical, temp)
                                if fingerprint(temp) != key:
                                    raise RuntimeError('Store copy verification failed')
                                os.replace(temp, stored)
                            finally:
                                temp.unlink(missing_ok=True)
                        if stored.is_symlink() or fingerprint(stored) != key:
                            raise RuntimeError(f'Invalid store object: {stored}')
                        source = stored
                    else:
                        source = canonical
                        if source.stat().st_dev != target.stat().st_dev:
                            item['skipped'] = 'different filesystems'
                            results.append(item)
                            continue
                    if args.method == 'clone':
                        try:
                            clone_replace(source, target)
                        except FileNotFoundError:
                            missing_event(target, 'clone preparation')
                            item['skipped'] = 'source or target disappeared before replacement'
                            results.append(item)
                            continue
                        except OSError as error:
                            if error.errno not in {errno.ENOTSUP, errno.EXDEV, errno.ENOSYS}:
                                raise
                            item['skipped'] = f'clone unavailable: {error}'
                            results.append(item)
                            continue
                    else:
                        replace_link(source, target, args.method)
                    item['applied'] = True
                    item['verified'] = True
                    print(json.dumps({'event': 'verified', 'path': str(target), 'method': args.method}), file=sys.stderr, flush=True)
                results.append(item)
    summary = {
        'purge_directories': len(stale_roots),
        'dedupe_files': sum(r['action'] == 'dedupe' for r in results),
        'estimated_dedupe_bytes': dedupe_bytes,
        'estimated_purge_bytes': purge_bytes,
        'estimated_combined_bytes': purge_bytes + retained_dedupe_bytes,
        'estimate_note': 'Logical file bytes, not physical disk savings. Existing multiply-linked files excluded; filesystem clones, compression, metadata and symlink overhead are not measured. Combined assumes purge then dedupe of retained projects.',
    }
    emit_report(args, {'status': 'completed', 'apply': args.apply, 'dependencies': len(dependencies), 'method': args.method, 'summary': summary, 'actions': results})
    return results


def private_json(path, payload):
    temporary = path.with_suffix('.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def emit_report(args, payload):
    path = getattr(args, '_report_path', None)
    if path is not None:
        private_json(path, payload)
    print(json.dumps(payload, indent=2), flush=True)


class ProgressTee:
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        return self.terminal.write(text)

    def flush(self):
        self.log.flush()
        self.terminal.flush()


@contextlib.contextmanager
def reporting(args):
    configured = getattr(args, 'report_dir', None)
    if configured is None:  # Internal callers may opt out; CLI always supplies a directory.
        yield
        return
    directory = Path(configured).expanduser().absolute()
    if any(part in {'node_modules', '__pycache__', '.venv', 'venv', 'env'} for part in directory.parts):
        raise ValueError('Report directory must be outside dependency directories')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    args._report_dir = directory
    args._report_path = directory / (run_id + '.json')
    log_path = directory / (run_id + '.log')
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as log:
        try:
            with contextlib.redirect_stderr(ProgressTee(sys.stderr, log)):
                yield
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as error:
            log.write(json.dumps({'event': 'aborted', 'error': str(error) or 'interrupted'}) + '\n')
            log.flush()
            # The progress journal remains available even if the report write fails.
            events = []
            for line in log_path.read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            private_json(args._report_path, {'status': 'aborted', 'error': str(error) or 'interrupted',
                         'apply': args.apply, 'events': events, 'progress_log': str(log_path)})
            raise
        finally:
            print(f'node-mash report: {args._report_path}', file=sys.stderr, flush=True)
            args._report_path = None
            args._report_dir = None


def run(args):
    with reporting(args):
        return _run(args)


def scan(args):
    with reporting(args):
        store = Path(args.store).expanduser().absolute()
        reports = getattr(args, '_report_dir', None)
        emit_report(args, [{'path': str(p), 'project_modified': activity(p.parent, store, reports)}
                          for p in discover([Path(r).expanduser().absolute() for r in args.roots], store, reports)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['scan', 'dedupe', 'purge', 'service'])
    parser.add_argument('roots', nargs='+')
    parser.add_argument('--apply', action='store_true', help='Enable filesystem changes')
    parser.add_argument('--days', type=float, default=180)
    parser.add_argument('--method', choices=['clone', 'hardlink', 'symlink'], default='clone')
    parser.add_argument('--report-dir', default='~/.local/state/node-mash/reports', help='Directory for per-run JSON reports and progress logs')
    parser.add_argument('--store', default='~/.local/share/node-mash/objects')
    parser.add_argument('--min-bytes', type=int, default=4096)
    parser.add_argument('--interval', type=float, default=86400, help='Service interval in seconds')
    parser.add_argument('--once', action='store_true', help='Run one service cycle and exit')
    parser.add_argument('--purge', action='store_true', help='Also enable age-based deletion in service mode')
    args = parser.parse_args()
    if args.days <= 0 or args.interval <= 0 or args.min_bytes < 0:
        parser.error('days and interval must be positive; min-bytes must be nonnegative')
    try:
        while True:
            if args.command == 'scan':
                scan(args)
            else:
                run(args)
            if args.command != 'service' or args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'node-mash: {error}\n')


if __name__ == '__main__':
    main()
