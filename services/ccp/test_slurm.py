"""Slurm builder tests: slurm.conf/gres.conf generation from hardware
fixtures, the generate/deploy API (guards, storage, playbook contents), and
lifecycle advancement on successful deploy.
"""
import os, sys, time, json, tempfile, pathlib

TMP = tempfile.mkdtemp(prefix='ccp_slurm_')
os.environ.update({
    'CCP_DB': f'{TMP}/ccp.db',
    'CCP_FILES_DIR': f'{TMP}/files',
    'CCP_JOBS_DIR': f'{TMP}/jobs',
    'CCP_SSH_KEY': f'{TMP}/ssh/id_ccp',
    'CCP_ADMIN_USER': 'admin',
    'CCP_ADMIN_PASSWORD': 'adminpass123',
    'CCP_TEST_SYNC_JOBS': '1',
})
pathlib.Path(f'{TMP}/ssh').mkdir(parents=True)
pathlib.Path(f'{TMP}/ssh/id_ccp').write_text('FAKE\n')
pathlib.Path(f'{TMP}/ssh/id_ccp.pub').write_text('ssh-ed25519 AAAATEST ccp\n')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as ccp                                            # noqa: E402
import db                                                    # noqa: E402
import executor                                              # noqa: E402
import slurm                                                 # noqa: E402

ok = fail = 0

def check(name, cond, detail=''):
    global ok, fail
    if cond:
        ok += 1
        print(f'  PASS  {name}')
    else:
        fail += 1
        print(f'  FAIL  {name}  {detail}')

def client_for(username, password):
    c = ccp.app.test_client()
    r = c.post('/login', data={'username': username, 'password': password})
    assert r.status_code in (302, 303)
    with c.session_transaction() as s:
        csrf = s['csrf']
    return c, {'X-CSRF-Token': csrf}

print('== pure generation ==')
members = [
    {'id': 1, 'name': 'rack0_sled1_gpu', 'address': '192.168.100.21'},
    {'id': 2, 'name': 'rack0_sled2_gpu', 'address': '192.168.100.22'},
    {'id': 3, 'name': 'rack0_sled3_cpu', 'address': '192.168.100.23'},
]
hw = {
    1: {'cpu_cores': 128, 'cpu_sockets': 2, 'threads_per_core': 2,
        'mem_mb': 515899, 'gpu_count': 4},
    2: {'cpu_cores': 128, 'cpu_sockets': 2, 'threads_per_core': 2,
        'mem_mb': 515899, 'gpu_count': 1},
    3: {'cpu_cores': 7, 'cpu_sockets': 1, 'threads_per_core': 2,   # 7 % 2 != 0
        'mem_mb': 15923, 'gpu_count': 0},
}
conf, gres, warnings = slurm.generate('ai-train', members, members[0], hw)
check('controller line with address',
      'SlurmctldHost=rack0_sled1_gpu(192.168.100.21)' in conf, conf)
check('gpu node line: CPUs/geometry/memory/gres',
      'NodeName=rack0_sled1_gpu NodeAddr=192.168.100.21 CPUs=128 Sockets=2 '
      'CoresPerSocket=32 ThreadsPerCore=2 RealMemory=515387 Gres=gpu:4 '
      'State=UNKNOWN' in conf, conf)
check('inconsistent geometry omitted, CPUs kept',
      'NodeName=rack0_sled3_cpu NodeAddr=192.168.100.23 CPUs=7 RealMemory=15411 '
      'State=UNKNOWN' in conf, conf)
check('GresTypes only when GPUs exist', 'GresTypes=gpu' in conf)
check('partition covers all members',
      'PartitionName=main Nodes=rack0_sled1_gpu,rack0_sled2_gpu,rack0_sled3_cpu '
      'Default=YES MaxTime=INFINITE State=UP' in conf, conf)
check('no warnings when facts exist', warnings == [])
check('gres multi-gpu range + single-gpu file',
      'NodeName=rack0_sled1_gpu Name=gpu File=/dev/nvidia[0-3]' in gres
      and 'NodeName=rack0_sled2_gpu Name=gpu File=/dev/nvidia0' in gres, gres)
check('cpu-only node absent from gres', 'sled3' not in gres)

print('== GPU declaration depends on the driver being loaded ==')
# verified against real slurm 23.11.4: declaring a GPU whose /dev/nvidia*
# does not exist makes slurmd hang on "Waiting for gres.conf file /dev/nvidia0"
gpu_pci = [{'id': 9, 'name': 'gpu-nodrv', 'address': '10.0.0.9'}]
hw_pci = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2,
              'raw_json': json.dumps({'pci_gpu': ['03:00.0 VGA: NVIDIA H100']})}}
c_pci, g_pci, w_pci = slurm.generate('x', gpu_pci, gpu_pci[0], hw_pci)
check('driverless GPU (lspci only) is NOT declared',
      'Gres=gpu' not in c_pci and 'GresTypes' not in c_pci and g_pci == '',
      (c_pci, g_pci))
check('driverless GPU warns with the reason and the remedy',
      w_pci and 'driver is not loaded' in w_pci[0] and 'rescan' in w_pci[0], w_pci)

hw_nvml = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2,
               'raw_json': json.dumps({'gpu': ['NVIDIA H100', 'NVIDIA H100']})}}
c_nv, g_nv, w_nv = slurm.generate('x', gpu_pci, gpu_pci[0], hw_nvml)
check('driver-detected GPU IS declared with device files',
      'Gres=gpu:2' in c_nv and 'GresTypes=gpu' in c_nv
      and 'File=/dev/nvidia[0-1]' in g_nv, (c_nv, g_nv))
check('driver-detected GPU raises no driver warning',
      not any('driver is not loaded' in w for w in w_nv), w_nv)

hw_bare = {9: {'cpu_cores': 8, 'mem_mb': 16000, 'gpu_count': 2}}  # no raw_json
c_b, g_b, _ = slurm.generate('x', gpu_pci, gpu_pci[0], hw_bare)
check('summary-only hardware still declares GPUs (back-compat)',
      'Gres=gpu:2' in c_b and 'File=/dev/nvidia[0-1]' in g_b)

conf2, gres2, warn2 = slurm.generate('cpu-only', [members[2]], members[2],
                                     {3: hw[3]})
check('no-GPU cluster: empty gres, no GresTypes',
      gres2 == '' and 'GresTypes' not in conf2)

conf3, _, warn3 = slurm.generate('bare', [members[1]], members[1], {})
check('missing hardware → warning + minimal defaults',
      warn3 and 'CPUs=1 RealMemory=256' in conf3, (warn3, conf3))

print('== deploy playbook ==')
pb = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu')
check('playbook inlines slurm.conf', 'ClusterName=ai-train' in pb)
check('SlurmctldHost name resolved to the real hostname at deploy time, '
      'address kept',
      "SlurmctldHost={{ hostvars[slurm_controller]['ansible_hostname'] }}"
      '(192.168.100.21)' in pb
      and 'SlurmctldHost=rack0_sled1_gpu' not in pb, pb)
check('stored conf keeps the inventory-name intent',
      'SlurmctldHost=rack0_sled1_gpu(192.168.100.21)' in conf)
check('MailProg pinned so minimal installs do not log errors',
      'MailProg=/bin/true' in conf)
check('playbook inlines gres.conf', '/dev/nvidia[0-3]' in pb)
check('munge key generated on controller and distributed',
      'mungekey' in pb and 'slurp' in pb and 'b64decode' in pb)
check('controller-only slurmctld', 'slurmctld' in pb
      and 'inventory_hostname == slurm_controller' in pb)
check('slurmctld disabled on non-controllers (no failed-everywhere noise)',
      'Disable slurmctld on non-controller nodes' in pb
      and 'inventory_hostname != slurm_controller' in pb, pb)
check('facts gathered for the SlurmctldHost resolution',
      'gather_facts: true' in pb)
check('version-skew gate present and runs before any deployment work',
      'Fail fast when Slurm versions differ' in pb
      and pb.index('Fail fast when Slurm versions differ')
          < pb.index('Generate the munge key'), 'gate must precede munge/config')
check('slurmd NodeName pinned via -N (independent of OS hostname)',
      'SLURMD_OPTIONS=-N {{ inventory_hostname }}' in pb
      and 'slurmd.service.d' in pb, pb)
check('slurmd start does a daemon-reload so the drop-in is read',
      'daemon_reload: true' in pb)
import yaml as _yaml
_docs = list(_yaml.safe_load_all(pb))
check('deploy playbook is valid YAML', _docs and isinstance(_docs[0], list), type(_docs[0]))
check('plain deploy does not purge anything',
      'Purge the installed Slurm' not in pb and 'state: absent' not in pb)

print('== a daemon that will not start explains itself in the job log ==')
# systemd's failure text is only "control process exited with error code";
# the rescue must surface the daemon's own reason (journal + foreground run)
_tasks_plain = _docs[0][0]['tasks']
sd = next(t for t in _tasks_plain if t['name'] == 'Start slurmd on every node')
check('slurmd start is a block with a rescue', 'block' in sd and 'rescue' in sd, list(sd))
check('the block restarts+enables slurmd with a daemon-reload',
      sd['block'][0]['ansible.builtin.systemd'] == {'name': 'slurmd', 'state': 'restarted',
                                                    'enabled': True, 'daemon_reload': True},
      sd['block'])
rescue_txt = str(sd['rescue'])
check('rescue collects status, unit state, THIS attempt\'s journal and the effective unit',
      'journalctl -u slurmd --since "-15 min"' in rescue_txt and 'systemctl status slurmd' in rescue_txt
      and 'systemctl show slurmd -p ActiveState' in rescue_txt
      and 'systemctl cat slurmd' in rescue_txt, rescue_txt)
check('rescue never quotes a journal older than this attempt (a stale restart loop '
      'would be mistaken for the reason)', 'journalctl -u slurmd -n ' not in rescue_txt)
check('rescue runs slurmd in the foreground with the pinned NodeName',
      'timeout 8 slurmd -D -vv -N {{ inventory_hostname }}' in rescue_txt, rescue_txt)
check('rescue prints the diagnostics, then still fails the host',
      sd['rescue'][-2]['ansible.builtin.debug'] == {'var': 'slurmd_diag.stdout_lines'}
      and 'ansible.builtin.fail' in sd['rescue'][-1], sd['rescue'])
check('the collection step can never mask the failure',
      sd['rescue'][0].get('failed_when') is False and sd['rescue'][0].get('changed_when') is False)
collect_sh = sd['rescue'][0]['ansible.builtin.shell']
check('journal fatal/error lines are extracted right before the probe',
      'journal errors' in collect_sh and "grep -iE 'fatal|error'" in collect_sh, collect_sh)
fail_msg = sd['rescue'][-1]['ansible.builtin.fail']['msg']
check('the final failure message itself carries the tail of the diagnostics '
      '(people copy the last red block)',
      'slurmd_diag.stdout_lines[-45:] | join(ccp_nl)' in fail_msg and 'own reason' in fail_msg, fail_msg)
check('join uses a YAML-defined real newline (Ansible escapes backslashes in Jinja)',
      _docs[0][0]['vars'].get('ccp_nl') == '\n', _docs[0][0]['vars'])
sc = next(t for t in _tasks_plain if t['name'] == 'Start slurmctld on the controller')
check('slurmctld start guarded to the controller and rescued too',
      sc.get('when') == 'inventory_hostname == slurm_controller' and 'rescue' in sc, sc)
check('slurmctld foreground probe runs as the slurm user (root-owned state files '
      'would break the real daemon)',
      'runuser -u slurm -- timeout 8 slurmctld -D -vv' in str(sc['rescue']), sc['rescue'])

print('== GPU device files are verified before anything is changed ==')
# slurmd waits 20 s for every File= device in gres.conf and then exits; a
# rebooted node whose driver is not loaded has no /dev/nvidia* at all.
def _idx(names, prefix):
    return next((i for i, n in enumerate(names) if n.startswith(prefix)), -1)
check('gres_devices expands File= ranges per node',
      slurm.gres_devices(gres) == {
          'rack0_sled1_gpu': ['/dev/nvidia0', '/dev/nvidia1', '/dev/nvidia2', '/dev/nvidia3'],
          'rack0_sled2_gpu': ['/dev/nvidia0']}, slurm.gres_devices(gres))
check('no gres.conf → nothing to verify', slurm.gres_devices('') == {})
names_plain = [t['name'] for t in _tasks_plain]
probe_i = _idx(names_plain, 'Probe GPU device files')
gpu_gate_i = _idx(names_plain, 'Fail when a declared GPU device file is missing')
check('probe + gate present when GPUs are declared',
      probe_i > -1 and gpu_gate_i > probe_i, names_plain)
probe_sh = _tasks_plain[probe_i]['ansible.builtin.shell']
check('probe warms the driver (nvidia-smi creates the device files) then lists them',
      'nvidia-smi -L' in probe_sh and 'ls /dev/nvidia[0-9]*' in probe_sh, probe_sh)
check('probe and gate only touch declared GPU nodes',
      _tasks_plain[probe_i]['when'] == 'inventory_hostname in ccp_gres_nodes')
check('declared device files are passed to the play',
      _docs[0][0]['vars']['ccp_gres_nodes'] == slurm.gres_devices(gres), _docs[0][0]['vars'])
check('GPU gate precedes every destructive task',
      gpu_gate_i < _idx(names_plain, 'Install munge and slurm-wlm'), (gpu_gate_i, names_plain))
gmsg = _tasks_plain[gpu_gate_i]['ansible.builtin.assert']['fail_msg']
check('gate message names node, missing files, present files and the fix',
      'gpu_missing_here' in gmsg and 'present now' in gmsg and 'nvidia-persistenced' in gmsg
      and 'Nothing was changed' in gmsg, gmsg)
cg_i = _idx(names_plain, 'Write cgroup.conf')
check('cgroup.conf written with the cgroup plugin disabled (matches linuxproc/none)',
      cg_i > -1 and 'CgroupPlugin=disabled' in _tasks_plain[cg_i]['ansible.builtin.copy']['content'])
check('cgroup.conf lands after the install and before slurmd starts',
      _idx(names_plain, 'Install munge and slurm-wlm') < cg_i < _idx(names_plain, 'Start slurmd on every node'))
pers_i = _idx(names_plain, 'Keep the NVIDIA device files across reboots')
check('nvidia-persistenced enabled best-effort on GPU nodes only',
      pers_i > -1 and _tasks_plain[pers_i].get('failed_when') is False
      and _tasks_plain[pers_i]['when'] == 'inventory_hostname in ccp_gres_nodes', _tasks_plain[pers_i] if pers_i > -1 else None)
check("rescue shows this node's gres lines and the NVIDIA device files",
      '/etc/slurm/gres.conf' in collect_sh and 'ls -l /dev/nvidia[0-9]*' in collect_sh)
d_nogpu = _yaml.safe_load(slurm.deploy_playbook(conf, '', 'rack0_sled1_gpu'))[0]
n_nogpu = [t['name'] for t in d_nogpu['tasks']]
check('no GPUs declared → no probe, no gate, empty map; cgroup.conf still written',
      not any(n.startswith(('Probe GPU', 'Fail when a declared GPU', 'Keep the NVIDIA')) for n in n_nogpu)
      and d_nogpu['vars']['ccp_gres_nodes'] == {} and _idx(n_nogpu, 'Write cgroup.conf') > -1, n_nogpu)
check('cleanup removes cgroup.conf too', '/etc/slurm/cgroup.conf' in slurm.cleanup_playbook())

print('== clean reinstall + version pin ==')
pb_re = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu',
                              reinstall=True, version='23.11.4-1.2ubuntu5')
tasks_re = _yaml.safe_load(pb_re)[0]['tasks']
names_re = [t['name'] for t in tasks_re]
check('reinstall playbook is valid YAML', isinstance(tasks_re, list))
check('services stopped before the purge',
      names_re.index('Stop Slurm and munge before the clean reinstall')
      < names_re.index('Purge the installed Slurm and munge packages'))
purge = tasks_re[names_re.index('Purge the installed Slurm and munge packages')]
check('purge removes packages with purge+autoremove',
      purge['ansible.builtin.apt']['state'] == 'absent'
      and purge['ansible.builtin.apt']['purge'] is True
      and purge['ansible.builtin.apt']['autoremove'] is True, purge)
leftovers = tasks_re[names_re.index('Remove leftover Slurm and munge config and state')]
paths = leftovers['loop']
for p in ('/etc/slurm', '/etc/munge', '/var/spool/slurmctld', '/var/spool/slurmd'):
    check(f'purge clears {p}', p in paths, paths)
check('purge clears the CCP NodeName drop-in too',
      any('10-ccp-nodename.conf' in p for p in paths), paths)
inst_i = next(i for i, n in enumerate(names_re) if n.startswith('Install munge'))
inst = tasks_re[inst_i]['ansible.builtin.apt']
check('install is pinned to the requested version',
      'slurm-wlm=23.11.4-1.2ubuntu5' in inst['name'], inst)
check('pinned install allows a downgrade',
      inst.get('allow_downgrade') is True, inst)
check('purge happens before the install', inst_i > names_re.index(
      'Remove leftover Slurm and munge config and state'))
gate_i = next((i for i, n in enumerate(names_re)
               if n.startswith('Fail fast when Slurm versions differ')), -1)
# the gate must run BEFORE the install/purge (see the ordering block below);
# convergence is then confirmed again afterwards
check('version gate runs before the install, never after',
      -1 < gate_i < inst_i, (gate_i, inst_i))
check('a post-install confirmation re-checks convergence',
      'Confirm every node ended up on the same Slurm version' in names_re
      and names_re.index('Confirm every node ended up on the same Slurm version') > inst_i)
check('gate message points at reinstall/pin and the PXE fix',
      'Pin version' in pb_re and 'Clean reinstall' in pb_re
      and 'same Ubuntu release' in pb_re)

print('== nothing destructive may run before every gate has passed ==')
# Gating after the purge once left nodes with slurm-wlm installed, slurmd
# enabled and /etc/slurm deleted → slurmd went configless and looped on
# "resolve_ctls_from_dns_srv: Unknown host" forever.
def _first(names, prefix):
    return next((i for i, n in enumerate(names) if n.startswith(prefix)), -1)

DESTRUCTIVE = ('Stop Slurm and munge before the clean reinstall',
               'Purge the installed Slurm and munge packages',
               'Remove leftover Slurm and munge config and state',
               'Install munge and slurm-wlm')
for label, kw in (('plain', {}), ('reinstall', {'reinstall': True}),
                  ('pinned', {'version': '23.11.4-1'}),
                  ('reinstall+pin', {'reinstall': True, 'version': '23.11.4-1'})):
    names = [t['name'] for t in
             _yaml.safe_load(slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu', **kw))[0]['tasks']]
    gates = [i for i, n in enumerate(names)
             if n.startswith('Fail fast when Slurm versions differ')
             or n.startswith('Fail when the pinned version')
             or n.startswith('Fail when a declared GPU device file')]
    firsts = [_first(names, d) for d in DESTRUCTIVE]
    firsts = [i for i in firsts if i > -1]
    check(f'{label}: every gate precedes every destructive task',
          gates and firsts and max(gates) < min(firsts), (gates, firsts, names))
    hold = _first(names, 'Hold Slurm daemons down')
    write = _first(names, 'Write slurm.conf')
    inst = _first(names, 'Install munge and slurm-wlm')
    check(f'{label}: daemons held down between install and config',
          inst < hold < write, (inst, hold, write))
    check(f'{label}: slurmd only started after slurm.conf exists',
          write < _first(names, 'Start slurmd on every node'))

print('== source build: the identical upstream release on every node ==')
# Distro packages can never converge nodes on different Ubuntu releases (each
# ships only its own Slurm); building the same tarball everywhere can.
pb_src = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu', install_from='source')
tasks_src = _yaml.safe_load(pb_src)[0]['tasks']
names_src = [t['name'] for t in tasks_src]
check('source playbook is valid YAML', isinstance(tasks_src, list))
V = slurm.SOURCE_DEFAULT_VERSION
check('default upstream release is 25.11.8', V == '25.11.8')
check('toolchain and munge installed from the distro',
      'Install munge and the Slurm build toolchain' in names_src, names_src)
tool = tasks_src[names_src.index('Install munge and the Slurm build toolchain')]
check('build deps cover compiler, munge headers, hwloc, pam and http-parser',
      all(p in tool['ansible.builtin.apt']['name'] for p in
          ('munge', 'build-essential', 'libmunge-dev', 'libhwloc-dev', 'libpam0g-dev',
           'libhttp-parser-dev')),
      tool)
rm_i = names_src.index('Remove distro Slurm packages so they cannot shadow the source build')
rm = tasks_src[rm_i]['ansible.builtin.apt']
check('distro slurm-wlm purged before the build so /usr/sbin/slurmd is ours',
      rm['state'] == 'absent' and rm['purge'] is True and 'slurm-wlm' in rm['name'], rm)
check('munge is NOT purged in source mode (it stays a distro package)',
      'munge' not in rm['name'], rm)
dl_i = names_src.index(f'Download the slurm-{V} source tarball')
dl = tasks_src[dl_i]['ansible.builtin.get_url']
check('tarball comes from SchedMD by default',
      dl['url'] == f'https://download.schedmd.com/slurm/slurm-{V}.tar.bz2', dl)
build_i = _first(names_src, f'Build and install slurm-{V} from source')
build = tasks_src[build_i]['ansible.builtin.shell']
check('build task present after the download', build_i > dl_i, (dl_i, build_i))
check('configure flags match the paths CCP writes (validated on Ubuntu 24.04)',
      '--prefix=/usr' in build and '--sysconfdir=/etc/slurm' in build
      and '--localstatedir=/var' in build and '--with-munge' in build, build)
check('build is parallel, installs the systemd units and refreshes ld cache',
      'make -j"$(nproc)"' in build and 'make install' in build
      and 'etc/slurmd.service etc/slurmctld.service /etc/systemd/system/' in build
      and 'ldconfig' in build, build)
check('build script aborts on the first failing step', 'set -euo pipefail' in build)
NEED = f"'slurm {V}' not in slurm_have.stdout or 'plugin=http_parser' not in slurm_have.stdout"
for i in (dl_i, build_i):
    check(f'{names_src[i][:30]}… skipped only when this release WITH the http_parser plugin is installed',
          tasks_src[i].get('when') == NEED, tasks_src[i].get('when'))
have_i = names_src.index('Check which Slurm is installed now')
check('installed-release probe runs after the purge, before the download, and tolerates absence',
      rm_i < have_i < dl_i and tasks_src[have_i].get('failed_when') is False)
check('probe reports the http_parser plugin (a build without it logs url_parser errors)',
      'slurmd -V' in tasks_src[have_i]['ansible.builtin.shell']
      and 'http_parser_libhttp_parser.so' in tasks_src[have_i]['ansible.builtin.shell']
      and 'echo plugin=http_parser' in tasks_src[have_i]['ansible.builtin.shell'])
check('slurm user created (packages used to do that)',
      'Ensure the slurm system user exists' in names_src
      and names_src.index('Ensure the slurm system user exists') > build_i)
check('systemd reloaded so the source-built units are known before they are used',
      'Reload systemd so the source-built units are known' in names_src
      and names_src.index('Reload systemd so the source-built units are known')
          < _first(names_src, 'Hold Slurm daemons down'))
check('apt differ-gate is skipped (the build converges everyone)',
      any(t.get('when') is False for t in tasks_src
          if t['name'].startswith('Fail fast when Slurm versions differ')))
check('no pin gate in source mode',
      not any(n.startswith('Fail when the pinned version') for n in names_src))
check('post-build confirmation still proves convergence',
      names_src.index('Confirm every node ended up on the same Slurm version') > build_i)
check('no distro slurm-wlm install task in source mode',
      not any(n.startswith('Install munge and slurm-wlm') for n in names_src))
# ordering invariants, same as the apt modes
SRC_DESTRUCTIVE = ('Stop Slurm and munge before the clean reinstall',
                   'Purge the installed Slurm and munge packages',
                   'Remove leftover Slurm and munge config and state',
                   'Install munge and the Slurm build toolchain',
                   'Remove distro Slurm packages')
for label, kw in (('source', {}), ('source+reinstall', {'reinstall': True})):
    kw = dict(kw, install_from='source')
    names = [t['name'] for t in
             _yaml.safe_load(slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu', **kw))[0]['tasks']]
    probes = [i for i, n in enumerate(names) if n.startswith('Record the installed Slurm version')
              or n.startswith('List the Slurm versions')]
    firsts = [i for i in (_first(names, d) for d in SRC_DESTRUCTIVE) if i > -1]
    check(f'{label}: read-only preflight precedes every destructive task',
          probes and firsts and max(probes) < min(firsts), (probes, firsts))
    tool_i = _first(names, 'Install munge and the Slurm build toolchain')
    hold = _first(names, 'Hold Slurm daemons down')
    write = _first(names, 'Write slurm.conf')
    check(f'{label}: daemons held down between build and config',
          tool_i < _first(names, 'Build and install') < hold < write,
          (tool_i, hold, write))
    check(f'{label}: slurmd only started after slurm.conf exists',
          write < _first(names, 'Start slurmd on every node'))
    if kw.get('reinstall'):
        check('reinstall purge runs before the toolchain install',
              _first(names, 'Remove leftover Slurm and munge config and state') < tool_i)
pb_mirror = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu', install_from='source',
                                  version='25.05.3',
                                  tarball_url='http://10.10.90.1:8080/slurm-25.05.3.tar.bz2')
tm = _yaml.safe_load(pb_mirror)[0]['tasks']
dlm = next(t for t in tm if t['name'].startswith('Download the slurm-25.05.3'))
check('custom release + local mirror URL honoured (air-gapped labs)',
      dlm['ansible.builtin.get_url']['url'] == 'http://10.10.90.1:8080/slurm-25.05.3.tar.bz2'
      and dlm['ansible.builtin.get_url']['dest'] == '/usr/local/src/slurm-25.05.3.tar.bz2'
      and any(t['name'].startswith('Build and install slurm-25.05.3') for t in tm), dlm)
check('idempotency guard follows the custom release',
      dlm.get('when') == "'slurm 25.05.3' not in slurm_have.stdout or 'plugin=http_parser' not in slurm_have.stdout",
      dlm.get('when'))

print('== version probing must not fail on a node without slurm installed ==')
names_t = _yaml.safe_load(slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu'))[0]['tasks']
probe = next(t for t in names_t if t['name'] == 'Record the installed Slurm version')
check('installed-version probe tolerates a fresh node',
      probe.get('failed_when') is False, probe)

print('== pinned deploy checks the pin is installable everywhere ==')
pb_pin = slurm.deploy_playbook(conf, gres, 'rack0_sled1_gpu', version='23.11.4-1')
tp = _yaml.safe_load(pb_pin)[0]['tasks']
np_ = [t['name'] for t in tp]
check('pin-availability gate present', 'Fail when the pinned version cannot be '
      'installed everywhere' in np_, np_)
check('pin gate names the offending nodes and the common versions',
      'is not in the apt sources of' in pb_pin and 'available on EVERY node' in pb_pin)
check('the differ-gate is skipped when a pin will converge the fleet',
      any(t.get('when') is False for t in tp
          if t['name'].startswith('Fail fast when Slurm versions differ')), tp)

print('== cleanup is a real recovery path ==')
cl = slurm.cleanup_playbook()
check('cleanup disables the daemons (stops configless slurmd looping)',
      'enabled: false' in cl and 'slurmd' in cl)
check('cleanup removes the CCP NodeName drop-in',
      '10-ccp-nodename.conf' in cl)
check('cleanup removes the CCP /etc/hosts block',
      'CCP CLUSTER HOSTS' in cl and 'state: absent' in cl)
check('cleanup playbook is valid YAML', isinstance(_yaml.safe_load(cl), list))

print('== the gate tells the operator which version to pin ==')
avail_i = next((i for i, n in enumerate(names_re)
                if n.startswith("List the Slurm versions")), -1)
seed_i = names_re.index('Seed the cluster-wide version candidate list')
red_i = names_re.index('Reduce it to versions available on every node')
check('candidate versions collected per node with a version sort',
      avail_i > -1 and 'apt-cache madison slurm-wlm'
      in str(tasks_re[avail_i]) and 'sort -Vu' in str(tasks_re[avail_i]))
check('collection and intersection run before the gate',
      avail_i < seed_i < red_i < gate_i, (avail_i, seed_i, red_i, gate_i))
check('intersection uses the intersect filter across the other hosts',
      'intersect' in str(tasks_re[red_i])
      and 'ansible_play_hosts[1:]' in str(tasks_re[red_i]))
gate_msg = tasks_re[gate_i]['ansible.builtin.assert']['fail_msg']
check('failure lists installed AND offered versions per node',
      'Installed now' in gate_msg and "apt sources" in gate_msg
      and 'slurm_avail' in gate_msg, gate_msg)
check('failure branches on whether a common version exists',
      'slurm_common' in gate_msg and 'NO version available on every node' in gate_msg
      and 'exist on EVERY node' in gate_msg)
check('availability probe never fails the play',
      tasks_re[avail_i].get('failed_when') is False)
cleanup = slurm.cleanup_playbook()
check('cleanup stops services and removes configs',
      'slurmd' in cleanup and '/etc/slurm/slurm.conf' in cleanup)

print('== API ==')
admin, ah = client_for('admin', 'adminpass123')
executor._password_ssh = lambda *a, **kw: (0, '')
executor._key_ssh = lambda a, u, p, cmd: (0, 'CCP_OK\nx\n')

cid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'ai-train', 'kind': 'slurm'}).get_json()['id']
gid = admin.post('/api/clusters', headers=ah,
                 json={'name': 'plain', 'kind': 'generic'}).get_json()['id']
r = admin.post(f'/api/clusters/{gid}/slurm/generate', headers=ah, json={})
check('generic cluster refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah, json={})
check('no members → 400', r.status_code == 400)

def mk(name, addr, state='managed'):
    nid = db.execute(
        "INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state,cluster_id) "
        "VALUES (?,?,'ssh','root',22,'',?,?,?)",
        (name, addr, int(time.time()), state, cid))
    return nid

n1 = mk('rack1_sled1_gpu', '10.0.1.1')
n2 = mk('rack1_sled2_gpu', '10.0.1.2')
n3 = mk('pending-node', '10.0.1.3', state='discovered')
db.execute('INSERT INTO hardware (node_id, cpu_cores, mem_mb, gpu_count, updated_at) '
           'VALUES (?,?,?,?,?)', (n1, 64, 256000, 2, int(time.time())))

r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah,
               json={'controller_node_id': n3})
check('non-managed controller refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/generate', headers=ah,
               json={'controller_node_id': n1})
d = r.get_json()
check('generate stores configs + warnings for unscanned member',
      r.status_code == 200 and 'ClusterName=ai-train' in d['slurm_conf']
      and any('rack1_sled2_gpu' in w for w in d['warnings']), d)
c = db.query('SELECT * FROM clusters WHERE id=?', (cid,), one=True)
check('configs persisted with controller',
      c['slurm_conf'] and c['controller_node_id'] == n1)
check('discovered member excluded from configs', 'pending-node' not in c['slurm_conf'])

print('== deploy ==')
r = admin.post(f'/api/clusters/{gid}/slurm/deploy', headers=ah)
check('deploy on generic cluster refused', r.status_code == 400)
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah)
check('deploy accepted', r.status_code == 202, r.get_json())
jid = r.get_json()['job_id']
job = db.query('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
spec = json.loads(job['spec'])
check('deploy job is a playbook run against the managed members',
      job['kind'] == 'slurm_deploy' and sorted(spec['node_ids']) == [n1, n2],
      spec)
check('playbook embedded in spec carries the generated conf',
      'ClusterName=ai-train' in spec['playbook'])
# ansible-playbook isn't installed in the test env: the job fails at exec —
# lifecycle must NOT advance on a failed deploy
c = db.query('SELECT * FROM clusters WHERE id=?', (cid,), one=True)
check('failed deploy does not advance lifecycle', c['slurm_state'] == 'INIT',
      c['slurm_state'])
check('apt deploy uses the default job budget', 'timeout' not in spec)
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'version': 'not a version!'})
check('malformed apt version refused', r.status_code == 400, r.get_json())

print('== deploy from source ==')
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'source'})
check('source deploy accepted', r.status_code == 202, r.get_json())
spec_s = json.loads(db.query('SELECT spec FROM jobs WHERE id=?',
                             (r.get_json()['job_id'],), one=True)['spec'])
check('source deploy gets an hour-long budget (compiling on every node)',
      spec_s.get('timeout') == 3600, spec_s.get('timeout'))
check('playbook builds the default release',
      f'Build and install slurm-{slurm.SOURCE_DEFAULT_VERSION} from source' in spec_s['playbook']
      and 'download.schedmd.com' in spec_s['playbook'])
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'source', 'version': '25.05.3',
                     'tarball_url': 'http://10.10.90.1:8080/slurm-25.05.3.tar.bz2',
                     'reinstall': True})
check('source deploy with release + mirror accepted', r.status_code == 202, r.get_json())
spec_m = json.loads(db.query('SELECT spec FROM jobs WHERE id=?',
                             (r.get_json()['job_id'],), one=True)['spec'])
check('release and mirror reach the playbook, purge included',
      'http://10.10.90.1:8080/slurm-25.05.3.tar.bz2' in spec_m['playbook']
      and 'Build and install slurm-25.05.3' in spec_m['playbook']
      and 'Purge the installed Slurm and munge packages' in spec_m['playbook'])
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'source', 'version': '23.11.4-1.2ubuntu5'})
check('apt-style version refused for a source build', r.status_code == 400, r.get_json())
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'source', 'tarball_url': 'ftp://mirror/slurm.tar.bz2'})
check('non-http tarball URL refused', r.status_code == 400, r.get_json())
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'source', 'tarball_url': 'http://x/a b"; rm -rf /'})
check('tarball URL with shell metacharacters refused', r.status_code == 400, r.get_json())
r = admin.post(f'/api/clusters/{cid}/slurm/deploy', headers=ah,
               json={'install_from': 'rpm'})
spec_x = json.loads(db.query('SELECT spec FROM jobs WHERE id=?',
                             (r.get_json()['job_id'],), one=True)['spec'])
check('unknown install_from falls back to distro packages',
      r.status_code == 202 and 'Install munge and slurm-wlm' in spec_x['playbook']
      and 'timeout' not in spec_x)
audit = db.query("SELECT detail FROM audit WHERE action='slurm.deploy' ORDER BY id DESC LIMIT 4")
check('audit trail records the source build and release',
      any('from source 25.05.3' in a['detail'] for a in audit)
      and any(f'from source {slurm.SOURCE_DEFAULT_VERSION}' in a['detail'] for a in audit),
      [a['detail'] for a in audit])

print('== lifecycle advance on success ==')
jid2 = executor.start_job('shell', 'x', {'node_ids': [], 'command': 'true',
                                         'cluster_id': cid, 'advance_to': 'DEPLOY'},
                          'admin')
# shell with no nodes exits 2 → no advance
check('failed job never advances', db.query(
    'SELECT slurm_state FROM clusters WHERE id=?', (cid,), one=True)['slurm_state'] == 'INIT')
lid = db.execute("INSERT INTO nodes (name,address,conn,ssh_user,ssh_port,groups,created_at,state) "
                 "VALUES ('local-x','localhost','local','root',22,'',?, 'managed')",
                 (int(time.time()),))
executor.start_job('shell', 'x', {'node_ids': [lid], 'command': 'true',
                                  'cluster_id': cid, 'advance_to': 'DEPLOY'}, 'admin')
check('successful job advances lifecycle', db.query(
    'SELECT slurm_state FROM clusters WHERE id=?', (cid,), one=True)['slurm_state'] == 'DEPLOY')

print('== RBAC ==')
admin.post('/api/users', headers=ah,
           json={'username': 'v3', 'password': 'password123', 'role': 'viewer'})
viewer, vh = client_for('v3', 'password123')
r = viewer.post(f'/api/clusters/{cid}/slurm/generate', headers=vh, json={})
check('viewer cannot generate', r.status_code == 403)
r = viewer.post(f'/api/clusters/{cid}/slurm/deploy', headers=vh)
check('viewer cannot deploy', r.status_code == 403)

print(f'\n{ok} passed, {fail} failed')
sys.exit(1 if fail else 0)
