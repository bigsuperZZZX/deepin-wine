from collections import defaultdict
import re
import copy

NAME_SELECTOR = re.compile(r' ([a-z\d][a-z\d.+\-]+)(?: :(\S+))?(?: \( ([<=>]+) (\S+) \))? '.replace(' ', r'\s*'))


def split_items(sep, text):
    return () if text is None else [x.strip() for x in text.split(sep)]


def compare_ver_digit(x, y):
    x = int(x or '0')
    y = int(y or '0')
    return (x > y) - (x < y)


def compare_ver_non_digit(x, y):
    tx = re.sub(r'\D', '3', re.sub(r'[a-zA-Z]', '2', x.replace('~', '0'))) + '1'
    ty = re.sub(r'\D', '3', re.sub(r'[a-zA-Z]', '2', y.replace('~', '0'))) + '1'
    if tx < ty:
        return -1
    elif tx > ty:
        return 1
    else:
        return (x > y) - (x < y)


def compare_version(x, y):
    pattern = re.compile(r'(\d*)(\D*)')
    lx = re.findall(pattern, x)
    ly = re.findall(pattern, y)
    for (dx, nx), (dy, ny) in zip(lx, ly):
        cmp = compare_ver_digit(dx, dy)
        if cmp:
            return cmp
        cmp = compare_ver_non_digit(nx, ny)
        if cmp:
            return cmp
    len_lx = len(lx)
    len_ly = len(ly)
    return (len_lx > len_ly) - (len_lx < len_ly)


def compare_full_version(x, op, y):
    pattern = re.compile(r'(?:(\d+):)?(.+?)(?:-([^-]+))?')
    epoch_x, version_x, revision_x = re.fullmatch(pattern, x).groups()
    epoch_y, version_y, revision_y = re.fullmatch(pattern, y).groups()
    cmp = compare_ver_digit(epoch_x, epoch_y)
    if not cmp:
        cmp = compare_version(version_x, version_y)
        if not cmp:
            cmp = compare_version(revision_x or '0', revision_y or '0')

    supported_op = (
        ('<=', '=', '>='),
        ('>>', '>='),
        ('<<', '<=')
    )
    return op in supported_op[cmp]


class Package:
    def __init__(self, f):
        lines = self.lines = []
        while True:
            line = f.readline()
            if line.isspace():
                if lines:
                    break
            elif not line:
                if lines:
                    if not lines[-1].endswith('\n'):
                        lines[-1] += '\n'
                    break
                raise StopIteration
            elif line[0].isspace():
                lines[-1] += line
            else:
                lines.append(line)

    def __str__(self):
        return ''.join(self.lines)

    def __repr__(self):
        return '%s:%s v%s' % (self['Package'], self['Architecture'], self['Version'])

    def _search_filed(self, key):
        key = key.lower()
        for i, line in enumerate(self.lines):
            k, v = line.split(':', 1)
            if k.lower() == key:
                return i, k, v.strip()
        return None, None, None

    def __getitem__(self, key):
        return self._search_filed(key)[2]

    def __setitem__(self, key, set_value):
        i, k, v = self._search_filed(key)
        self.lines[i] = '%s: %s\n' % (k, set_value(v))


def make_repo_meta(packages_path):
    with open(packages_path, 'rt', errors='ignore') as f:
        entries = defaultdict(list)
        try:
            while True:
                offset = f.tell()
                pkg = Package(f)
                entries[pkg['Package']].append(offset)
                for provide in split_items(',', pkg['Provides']):
                    m = re.fullmatch(NAME_SELECTOR, provide)
                    entries[m.group(1)].append(offset)
        except StopIteration:
            return dict(entries)


# TODO: check pkg version and arch
class Site:
    def __init__(self, name=None):
        self.name = name
        self.updated = False
        self.length = 0
        self.url_list = []
        self.path_list = []
        self.meta_list = []
        self.file_list = None
        self.visited = None
        self.broken = None

    def __getitem__(self, item):
        name, arch, op, version = item
        entries = []
        for i in range(self.length):
            file = self.file_list[i]
            for offset in self.meta_list[i].get(name, ()):
                index = i | (offset << 8)
                file.seek(offset)
                pkg = Package(file)

                pkg_arch = pkg['Architecture']
                if pkg_arch != 'all' and arch is not None and arch != pkg_arch:
                    continue

                ok_version = op is None
                if not ok_version:
                    if pkg['Package'] == name:
                        ok_version = compare_full_version(pkg['Version'], op, version)
                    if not ok_version:
                        for provide in split_items(',', pkg['Provides']):
                            p_name, _, _, p_version = re.fullmatch(NAME_SELECTOR, provide).groups()
                            if p_name == name and compare_full_version(p_version, op, version):
                                ok_version = True
                                break

                if ok_version:
                    entries.append((index, pkg))
        return entries

    def add(self, path, url=None, updated=False, meta=None):
        self.updated |= updated
        self.length += 1
        assert self.length <= (1 << 8)
        self.url_list.append(url)
        self.path_list.append(path)
        if meta is None:
            meta = make_repo_meta(path)
        self.meta_list.append(meta)

    def open(self, use_copy):
        if use_copy:
            site = copy.copy(self)
            site.visited = set()
            site.broken = set()
        else:
            site = self
        site.file_list = [open(path, 'rt', errors='ignore') for path in site.path_list]
        return site

    def close(self):
        for f in self.file_list:
            f.close()

    def diff_site(self, dest, full_selector, base_arch=None):
        all_broken_chains = []
        for and_selector in split_items(',', full_selector):

            any_ok = False
            broken_chains = []
            for selector in split_items('|', and_selector):
                name, arch, op, version = re.fullmatch(NAME_SELECTOR, selector).groups()
                arch = arch or base_arch
                arch = None if arch in ('all', 'any') else arch

                if dest[name, arch, op, version]:
                    any_ok = True
                    continue

                selected = self[name, arch, op, version]
                if not selected:
                    broken_chains.append(selector)
                    continue

                for index, pkg in selected:
                    if index in self.visited:
                        any_ok = True
                        continue
                    self.visited.add(index)
                    pkg_arch = pkg['Architecture']
                    dep = self.diff_site(dest, pkg['Depends'], pkg_arch)
                    pre_dep = self.diff_site(dest, pkg['Pre-Depends'], pkg_arch)
                    if dep or pre_dep:
                        self.broken.add(index)
                        broken_chains.extend('%s <- %s' % (x, selector) for x in dep + pre_dep)
                    else:
                        any_ok = True

            if not any_ok:
                all_broken_chains.extend(broken_chains)
        return all_broken_chains

    def dump(self, index_list, f):
        for index in index_list:
            i = index & 0xff
            offset = index >> 8
            file = self.file_list[i]
            file.seek(offset)
            pkg = Package(file)
            pkg['Filename'] = lambda v: self.url_list[i].replace('://', '/') + '/' + v
            f.write(str(pkg))
            f.write('\n')
