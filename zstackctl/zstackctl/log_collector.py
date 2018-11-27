#!/usr/bin/env python
# encoding: utf-8

import yaml
from zstacklib import *
import threading
from utils import shell
from utils.sql_query import MySqlCommandLineQuery
from termcolor import colored
from datetime import datetime, timedelta


def info_verbose(*msg):
    if len(msg) == 1:
        out = '%s\n' % ''.join(msg)
    else:
        out = ''.join(msg)
    now = datetime.now()
    out = "%s " % str(now) + out
    sys.stdout.write(out)


def collect_fail_verbose(*msg):
    if len(msg) == 1:
        out = '%s\n' % ''.join(msg)
    else:
        out = ''.join(msg)
    now = datetime.now()
    out = "%s " % str(now) + out
    return out


class CtlError(Exception):
    pass


def get_default_ip():
    cmd = shell.ShellCmd("""dev=`ip route|grep default|head -n 1|awk -F "dev" '{print $2}' | awk -F " " '{print $1}'`; ip addr show $dev |grep "inet "|awk '{print $2}'|head -n 1 |awk -F '/' '{print $1}'""")
    cmd(False)
    return cmd.stdout.strip()


def decode_conf_yml(args):
    base_conf_path = '/var/lib/zstack/virtualenv/zstackctl/lib/python2.7/site-packages/zstackctl/conf/'
    default_yml_mn_only = 'collect_log_mn_only.yml'
    default_yml_mn_db = 'collect_log_mn_db.yml'
    default_yml_full = 'collect_log_full.yml'
    default_yml_full_db = 'collect_log_full_db.yml'
    default_yml_mn_host = "collect_log_mn_host.yml"
    yml_conf_dir = None
    name_array = []

    if args.mn_only:
        yml_conf_dir = base_conf_path + default_yml_mn_only
    elif args.mn_db:
        yml_conf_dir = base_conf_path + default_yml_mn_db
    elif args.full:
        yml_conf_dir = base_conf_path + default_yml_full
    elif args.full_db:
        yml_conf_dir = base_conf_path + default_yml_full_db
    elif args.mn_host:
        yml_conf_dir = base_conf_path + default_yml_mn_host
    else:
        if args.p is None:
            yml_conf_dir = base_conf_path + default_yml_full
        else:
            yml_conf_dir = args.p

    decode_result = {}
    decode_error = None
    f = open(yml_conf_dir)
    try:
        conf_dict = yaml.load(f)
    except:
        decode_error = 'decode yml error,please check the yml'
        decode_result['decode_error'] = decode_error
        return decode_result

    for conf_key, conf_value in conf_dict.items():
        collect_type = conf_key
        list_value = conf_value.get('list')
        logs = conf_value.get('logs')
        if list_value is None or logs is None:
            decode_error = 'host or log can not be empty in %s' % log
            break
        else:
            if '\n' in list_value:
                temp_array = list_value.split('\n')
                conf_value['list'] = temp_array
            elif ',' in list_value:
                temp_array = list_value.split(',')
                conf_value['list'] = temp_array
            else:
                if ' ' in list_value:
                    temp_array = list_value.split()
                    conf_value['list'] = temp_array
        for log in logs:
            name_value = log.get('name')
            dir_value = log.get('dir')
            file_value = log.get('file')
            exec_value = log.get('exec')
            if name_value is None:
                decode_error = 'log name can not be None in %s' % log
                break
            else:
                if name_value in name_array:
                    decode_error = 'duplicate name key :%s' % name_value
                    break
                else:
                    name_array.append(name_value)
            if dir_value is None:
                if exec_value is None:
                    decode_error = 'dir, exec cannot be empty at the same time in  %s' % log
                    break
            else:
                if dir_value.startswith('/') is not True:
                    decode_error = 'dir must be an absolute path in %s' % log
                    break
                if file_value is not None and file_value.startswith('/'):
                    decode_error = 'file value can not be an absolute path in %s' % log
                    break
        decode_result[collect_type] = dict(
            (key, value) for key, value in conf_value.items() if key == 'list' or key == 'logs')
        name_array = []

    decode_result['decode_error'] = decode_error
    return decode_result


class CollectFromYml(object):
    failed_flag = False
    f_date = None
    t_date = None
    since = None
    logger_dir = '/var/log/zstack/'
    logger_file = 'zstack-ctl.log'
    zstack_log_dir = "/var/log/zstack/"
    success_count = 0
    fail_count = 0
    threads = []
    local_type = 'local'
    host_type = 'host'
    lock = threading.Lock()
    fail_list = {}
    ha_conf_dir = "/var/lib/zstack/ha/"
    ha_conf_file = ha_conf_dir + "ha.yaml"
    check = False
    check_result = {}

    def __init__(self, ctl, collect_dir, detail_version, time_stamp, args):
        self.ctl = ctl
        self.run(collect_dir, detail_version, time_stamp, args)


    def get_host_list(self, query_sql):
        query = MySqlCommandLineQuery()
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        query = MySqlCommandLineQuery()
        query.host = db_hostname
        query.port = db_port
        query.user = db_user
        query.password = db_password
        query.table = 'zstack'
        query.sql = query_sql
        return query.query()

    def build_collect_cmd(self, dir_value, file_value, collect_dir):
        cmd = 'find %s' % dir_value
        if file_value is not None:
            if file_value.startswith('regex='):
                cmd = cmd + ' -regex \'%s\'' % file_value
            else:
                cmd = cmd + ' -name \'%s\'' % file_value
        cmd = cmd + ' -exec ls --full-time {} \; | sort -k6 | awk \'{print $6\":\"$7\"|\"$9\"|\"$5}\''
        cmd = cmd + ' | awk -F \'|\' \'BEGIN{preview=0;} {if(NR==1 && ( $1 > \"%s\" || (\"%s\" < $1 && $1  <= \"%s\"))) print $2\"|\"$3; \
                               else if ((\"%s\" < $1 && $1 <= \"%s\") || ( $1> \"%s\" && preview < \"%s\")) print $2\"|\"$3; preview = $1}\''\
              % (self.t_date, self.f_date, self.t_date, self.f_date, self.t_date, self.t_date, self.t_date)
        if self.check:
            cmd = cmd + '| awk -F \'|\' \'BEGIN{size=0;} \
                   {size = size + $2/1024/1024;}  END{size=sprintf("%.1f", size); print size\"M\";}\''
        else:
            cmd = cmd + ' | awk -F \'|\' \'{print $1}\'| xargs -I {} /bin/cp -rf {} %s' % collect_dir
        return cmd

    def build_collect_cmd_old(self, dir_value, file_value, collect_dir):
        cmd = 'find %s' % dir_value
        if file_value is not None:
            if file_value.startswith('regex='):
                cmd = cmd + ' -regex \'%s\'' % file_value
            else:
                cmd = cmd + ' -name \'%s\'' % file_value
        if self.since is not None:
            cmd = cmd + ' -mtime -%s' % self.since
        if self.f_date is not None:
            cmd = cmd + ' -newermt \'%s\'' % self.f_date
        if self.t_date is not None:
            cmd = cmd + ' ! -newermt \'%s\'' % self.t_date
        if self.check:
            cmd = cmd + ' -exec ls -l {} \; | awk \'BEGIN{size=0;} \
                {size = size + $5/1024/1024;}  END{size=sprintf("%.1f", size); print size\"M\";}\''
        else:
            cmd = cmd + ' -exec /bin/cp -rf {} %s/ \;' % collect_dir
        return cmd

    def get_host_list(self, query_sql):
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        query = MySqlCommandLineQuery()
        query.host = db_hostname
        query.port = db_port
        query.user = db_user
        query.password = db_password
        query.table = 'zstack'
        query.sql = query_sql
        return query.query()

    def generate_host_post_info(self, host_ip, type):
        host_post_info = HostPostInfo()
        if type == "mn":
            if os.path.exists(self.ha_conf_file) is not True:
                raise CtlError('you are not in ha mode,please mn list use \'localhost\'')
            host_post_info.remote_user = 'root'
            # this will be changed in the future
            host_post_info.remote_port = '22'
            host_post_info.host = host_ip
            host_post_info.host_inventory = self.ha_conf_dir + 'host'
            host_post_info.post_url = ""
            host_post_info.private_key = self.ha_conf_dir + 'ha_key'
            return host_post_info
        # update inventory
        with open(self.ctl.zstack_home + "/../../../ansible/hosts") as f:
            old_hosts = f.read()
            if host_ip not in old_hosts:
                with open(self.ctl.zstack_home + "/../../../ansible/hosts", "w") as f:
                    new_hosts = host_ip + "\n" + old_hosts
                    f.write(new_hosts)
        (host_user, host_password, host_port) = self.get_host_ssh_info(host_ip, type)
        if host_user != 'root' and host_password is not None:
            host_post_info.become = True
            host_post_info.remote_user = host_user
            host_post_info.remote_pass = host_password
        host_post_info.remote_port = host_port
        host_post_info.host = host_ip
        host_post_info.host_inventory = self.ctl.zstack_home + "/../../../ansible/hosts"
        host_post_info.private_key = self.ctl.zstack_home + "/WEB-INF/classes/ansible/rsaKeys/id_rsa"
        host_post_info.post_url = ""
        return host_post_info

    def get_host_ssh_info(self, host_ip, type):
        db_hostname, db_port, db_user, db_password = self.ctl.get_live_mysql_portal()
        query = MySqlCommandLineQuery()
        query.host = db_hostname
        query.port = db_port
        query.user = db_user
        query.password = db_password
        query.table = 'zstack'
        if type == 'host':
            query.sql = "select * from HostVO where managementIp='%s'" % host_ip
            host_uuid = query.query()[0]['uuid']
            query.sql = "select * from KVMHostVO where uuid='%s'" % host_uuid
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['port']
            return (username, password, ssh_port)
        elif type == 'sftp-bs':
            query.sql = "select * from SftpBackupStorageVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == 'ceph-bs':
            query.sql = "select * from CephBackupStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "fs-bs":
            query.sql = "select * from FusionstorPrimaryStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == 'imageStore-bs':
            query.sql = "select * from ImageStoreBackupStorageVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['username']
            password = ssh_info['password']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "ceph-ps":
            query.sql = "select * from CephPrimaryStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "fs-ps":
            query.sql = "select * from FusionstorPrimaryStorageMonVO where hostname='%s'" % host_ip
            ssh_info = query.query()[0]
            username = ssh_info['sshUsername']
            password = ssh_info['sshPassword']
            ssh_port = ssh_info['sshPort']
            return (username, password, ssh_port)
        elif type == "vrouter":
            query.sql = "select value from GlobalConfigVO where name='vrouter.password'"
            password = query.query()
            username = "vyos"
            ssh_port = 22
            return (username, password, ssh_port)
        else:
            warn("unknown target type: %s" % type)

    def generate_tar_ball(self, run_command_dir, detail_version, time_stamp):
        info_verbose("Compressing log files ...")
        (status, output) = commands.getstatusoutput("cd %s && tar zcf collect-log-%s-%s.tar.gz collect-log-%s-%s"
                                                    % (run_command_dir, detail_version, time_stamp, detail_version, time_stamp))
        if status != 0:
            error("Generate tarball failed: %s " % output)

    def compress_and_fetch_log(self, local_collect_dir, tmp_log_dir, host_post_info):
        command = "cd %s && tar zcf ../collect-log.tar.gz . --ignore-failed-read --warning=no-file-changed || true" % tmp_log_dir
        run_remote_command(command, host_post_info)
        fetch_arg = FetchArg()
        fetch_arg.src = "%s../collect-log.tar.gz " % tmp_log_dir
        fetch_arg.dest = local_collect_dir
        fetch_arg.args = "fail_on_missing=yes flat=yes"
        fetch(fetch_arg, host_post_info)
        command = "rm -rf %s %s/../collect-log.tar.gz" % (tmp_log_dir, tmp_log_dir)
        run_remote_command(command, host_post_info)
        (status, output) = commands.getstatusoutput("cd %s && tar zxf collect-log.tar.gz" % local_collect_dir)
        if status != 0:
            warn("Uncompress %s/collect-log.tar.gz meet problem: %s" % (local_collect_dir, output))

        (status, output) = commands.getstatusoutput("rm -f %s/collect-log.tar.gz" % local_collect_dir)

    def add_collect_thread(self, type, params):
        if type == 'host':
            thread = threading.Thread(target=self.get_host_log, args=(params))
        elif type == 'local':
            thread = threading.Thread(target=self.get_local_log, args=(params))
        else:
            return
        thread.daemon = True
        self.threads.append(thread)

    def thread_run(self):
        for t in self.threads:
            t.start()
        for t in self.threads:
            t.join()

    def collect_configure_log(self, host_list, log_list, collect_dir, type):
        if isinstance(host_list, str):
            if host_list is None:
                return
            if host_list == 'localhost':
                self.add_collect_thread(self.local_type, [log_list, collect_dir, type])
                return
            else:
                self.add_collect_thread(self.host_type,
                                        [self.generate_host_post_info(host_list. type), log_list, collect_dir, type])
                return
        if isinstance(host_list, dict):
            if host_list['exec'] is not None:
                exec_cmd = host_list['exec'] + ' | awk \'NR>1\''
                try:
                    (status, output) = commands.getstatusoutput(exec_cmd)
                    if status == 0 and output.startswith('ERROR') is not True:
                        host_list = output.split('\n')
                    else:
                        raise CtlError('fail to exec %s' % host_list['exec'])
                except Exception:
                    raise CtlError('fail to exec %s' % host_list['exec'])
        host_list = list(set(host_list))
        for host_ip in host_list:
            if host_ip is None or host_ip == '':
                return
            if host_ip == 'localhost':
                self.add_collect_thread(self.local_type, [log_list, collect_dir, type])
            else:
                self.add_collect_thread(self.host_type, [self.generate_host_post_info(host_ip, type), log_list, collect_dir, type])

    def get_local_log(self, log_list, collect_dir, type):
        if self.check:
            for log in log_list:
                if 'exec' in log:
                    continue
                else:
                    command = self.build_collect_cmd(log['dir'], log['file'], None)
                    (status, output) = commands.getstatusoutput(command)
                    if status == 0:
                        key = "%s:%s:%s" % (type, 'localhost', log['name'])
                        self.check_result[key] = output
        else:
            local_collect_dir = None
            info_verbose("Collecting log from %s localhost ..." % type)
            local_collect_dir = collect_dir + '%s-%s/' % (type, get_default_ip())
            try:
                # file system broken shouldn't block collect log process
                if not os.path.exists(local_collect_dir):
                    os.makedirs(local_collect_dir)
                for log in log_list:
                    error_log_name = "%s:%s:%s" % (type, 'localhost', log['name'])
                    dest_log_dir = local_collect_dir
                    if 'name' in log:
                        dest_log_dir = local_collect_dir + '%s/' % log['name']
                        if not os.path.exists(dest_log_dir):
                            os.makedirs(dest_log_dir)
                    if 'exec' in log:
                        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                        command = log['exec']
                        file_path = dest_log_dir + '%s_%s' % (log['name'], timestamp) + '.gz'
                        cmd = shell.ShellCmd('%s | gzip > %s' % (command, file_path))
                        cmd(False)
                        if cmd.return_code == 0:
                            self.add_success_count()
                            logger.info("exec shell %s successfully!You can check the file at %s" % (command, file_path))
                        else:
                            self.add_fail_count(1, error_log_name, cmd.stderr)
                    else:
                        if os.path.exists(log['dir']):
                            command = self.build_collect_cmd(log['dir'], log['file'], dest_log_dir)
                            (status, output) = commands.getstatusoutput(command)
                            if status == 0:
                                self.add_success_count()
                                command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % dest_log_dir
                                (status, output) = commands.getstatusoutput(command)
                                if "The directory is empty" in output:
                                    warn("Didn't find log:%s on %s localhost" % (log['name'], type))
                                    logger.warn("Didn't find log:%s on %s" % (log['name'], type))
                            else:
                                self.add_fail_count(1, error_log_name, output)
                        else:
                            self.add_fail_count(1, error_log_name, "the dir path %s did't find on %s localhost" % (log['dir'], type))
                            logger.warn("the dir path %s did't find on %s localhost" % (log['dir'], type))
                            warn("the dir path %s did't find on %s localhost" % (log['dir'], type))
            except SystemExit:
                warn("collect log on localhost failed")
                logger.warn("collect log on localhost failed")
                command = 'rm -rf %s' % local_collect_dir
                self.failed_flag = True
                commands.getstatusoutput(command)
                return 1

            command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % local_collect_dir
            (status, output) = commands.getstatusoutput(command)
            if "The directory is empty" in output:
                warn("Didn't find log on localhost")
                command = 'rm -rf %s' % local_collect_dir
                commands.getstatusoutput(command)
                return 0

    def add_success_count(self):
        self.lock.acquire()
        self.success_count += 1
        self.lock.release()

    def add_fail_count(self, fail_log_number, fail_log_name, fail_cause):
        self.lock.acquire()
        try:
            self.fail_count += fail_log_number
            self.fail_list[collect_fail_verbose('[ ' + fail_log_name + ' ]')] = fail_cause
        except Exception:
            self.lock.release()
        self.lock.release()

    @ignoreerror
    def get_sharedblock_log(self, host_post_info, tmp_log_dir):
        info_verbose("Collecting sharedblock log from : %s ..." % host_post_info.host)
        target_dir = tmp_log_dir + "sharedblock"
        command = "mkdir -p %s " % target_dir
        run_remote_command(command, host_post_info)

        command = "lsblk -p -o NAME,TYPE,FSTYPE,LABEL,UUID,VENDOR,MODEL,MODE,WWN,SIZE > %s/lsblk_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "ls -l /dev/disk/by-id > %s/ls_dev_disk_by-id_info && echo || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "ls -l /dev/disk/by-path >> %s/ls_dev_disk_by-id_info && echo || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "multipath -ll -v3 >> %s/ls_dev_disk_by-id_info || true" % target_dir
        run_remote_command(command, host_post_info)

        command = "cp /var/log/sanlock.log* %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp /var/log/lvmlock/lvmlockd.log %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "lvmlockctl -i > %s/lvmlockctl_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "sanlock client status > %s/sanlock_client_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "sanlock client host_status> %s/sanlock_host_info || true" % target_dir
        run_remote_command(command, host_post_info)

        command = "lvs --nolocking -oall > %s/lvm_lvs_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "vgs --nolocking -oall > %s/lvm_vgs_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "lvmconfig --type diff > %s/lvm_config_diff_info || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp -r /etc/lvm/ %s || true" % target_dir
        run_remote_command(command, host_post_info)
        command = "cp -r /etc/sanlock %s || true" % target_dir
        run_remote_command(command, host_post_info)

    def check_host_reachable_in_queen(self, host_post_info):
        self.lock.acquire()
        result = check_host_reachable(host_post_info)
        self.lock.release()
        return result

    def get_host_log(self, host_post_info, log_list, collect_dir, type):
            if self.check_host_reachable_in_queen(host_post_info) is True:
                if self.check:
                    for log in log_list:
                        if 'exec' in log:
                            continue
                        else:
                            command = self.build_collect_cmd(log['dir'], log['file'], None)
                            (status, output) = run_remote_command(command, host_post_info, return_status=True,
                                                                  return_output=True)
                            if status is True:
                                key = "%s:%s:%s" % (type, host_post_info.host, log['name'])
                                self.check_result[key] = output
                else:
                    local_collect_dir = None
                    info_verbose("Collecting log from %s %s ..." % (type, host_post_info.host))
                    local_collect_dir = collect_dir + '%s-%s/' % (type, host_post_info.host)
                    tmp_log_dir = "%stmp-log/" % self.zstack_log_dir
                    try:
                        # file system broken shouldn't block collect log process
                        if not os.path.exists(local_collect_dir):
                            os.makedirs(local_collect_dir)
                        command = "mkdir -p %s " % tmp_log_dir
                        run_remote_command(command, host_post_info)
                        for log in log_list:
                            error_log_name = "%s:%s:%s" % (type, host_post_info.host, log['name'])
                            dest_log_dir = tmp_log_dir
                            if 'name' in log:
                                command = "mkdir -p %s" % tmp_log_dir + '/%s/' % log['name']
                                run_remote_command(command, host_post_info)
                                dest_log_dir = tmp_log_dir + '%s/' % log['name']
                            if 'exec' in log:
                                timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                                command = log['exec']
                                file_path = dest_log_dir + '%s_%s' % (log['name'], timestamp) + '.gz'
                                (status, output) = run_remote_command('%s | gzip > %s' % (command, file_path), \
                                                                      host_post_info, return_status=True, return_output=True)
                                if status is True:
                                    self.add_success_count()
                                    logger.info("exec shell %s successfully!You can check the file at %s" % (command, file_path))
                                else:
                                    self.add_fail_count(1, error_log_name, cmd.stderr)
                            else:
                                if file_dir_exist("path=%s" % log['dir'], host_post_info):
                                    command = self.build_collect_cmd(log['dir'], log['file'], dest_log_dir)
                                    (status, output) = run_remote_command(command, host_post_info, return_status=True, return_output=True)
                                    if status is True:
                                        self.add_success_count()
                                        command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % dest_log_dir
                                        (status, output) = run_remote_command(command, host_post_info, return_status=True, return_output=True)
                                        if "The directory is empty" in output:
                                            warn("Didn't find log:%s on %s %s" % (log['name'], type, host_post_info.host))
                                            logger.warn("Didn't find log:%s on %s %s" % (log['name'], type, host_post_info.host))
                                    else:
                                        self.add_fail_count(1, error_log_name, output)
                                else:
                                    self.add_fail_count(1, error_log_name, "the dir path %s did't find on %s %s" % (log['dir'], type, host_post_info.host))
                                    logger.warn("the dir path %s did't find on %s %s" % (log['dir'], type, host_post_info.host))
                                    warn("the dir path %s did't find on %s %s" % (log['dir'], type, host_post_info.host))
                        if type == 'host':
                            self.get_sharedblock_log(host_post_info, tmp_log_dir)
                    except SystemExit:
                        warn("collect log on host %s failed" % host_post_info.host)
                        logger.warn("collect log on host %s failed" % host_post_info.host)
                        command = 'rm -rf %s' % tmp_log_dir
                        self.failed_flag = True
                        run_remote_command(command, host_post_info)
                        return 1

                    command = 'test "$(ls -A "%s" 2>/dev/null)" || echo The directory is empty' % tmp_log_dir
                    (status, output) = run_remote_command(command, host_post_info, return_status=True, return_output=True)
                    if "The directory is empty" in output:
                        warn("Didn't find log on host: %s " % (host_post_info.host))
                        command = 'rm -rf %s' % tmp_log_dir
                        run_remote_command(command, host_post_info)
                        return 0
                    self.compress_and_fetch_log(local_collect_dir, tmp_log_dir, host_post_info)
            else:
                warn("Host %s is unreachable!" % host_post_info.host)
                self.add_fail_count(len(log_list), host_post_info.host, ("Host %s is unreachable!" % host_post_info.host))

    def get_total_size(self):
        values = self.check_result.values()
        total_size = 0
        for num in values:
            if num is None:
                continue
            elif num.endswith('K'):
                total_size += float(num[:-1])
            elif num.endswith('M'):
                total_size += float(num[:-1]) * 1024
            elif num.endswith('G'):
                total_size += float(num[:-1]) * 1024 * 1024
        total_size = str(round((total_size / 1024 / 1024), 2)) + 'G'
        print '%-50s%-50s' % ('TotalSize(exclude exec statements)', colored(total_size, 'green'))
        for key in sorted(self.check_result.keys()):
            print '%-50s%-50s' % (key, colored(self.check_result[key], 'green'))

    def format_date(self, str_date):
        d_arr = str_date.split('_')
        if len(d_arr) == 1 or len(d_arr) == 2:
            ymd_array = d_arr[0].split('-')
            if len(ymd_array) == 3:
                year = ymd_array[0]
                month = ymd_array[1]
                day = ymd_array[2]
                if len(d_arr) == 1:
                    return datetime(int(year), int(month), int(day)).strftime('%Y-%m-%d:%H:%M:%S')
                else:
                    hms_array = d_arr[1].split(':')
                    hour = hms_array[0] if len(hms_array) == 1 is not None else '00'
                    minute = hms_array[1] if len(hms_array) == 2 is not None else '00'
                    sec = hms_array[2] if len(hms_array) == 3 is not None else '00'
                    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(sec))\
                        .strftime('%Y-%m-%d:%H:%M:%S')
            else:
                raise CtlError('error datetime format:%s' % str_date)
        else:
            raise CtlError('error datetime format:%s' % str_date)

    def run(self, collect_dir, detail_version, time_stamp, args):
        run_command_dir = os.getcwd()
        if not os.path.exists(collect_dir) and args.check is not True:
            os.makedirs(collect_dir)

        if args.since is None:
            if args.from_date is None:
                self.f_date = (datetime.now() + timedelta(days=-1)).strftime('%Y-%m-%d:%H:%M')
            elif args.from_date == '-1':
                self.f_date = '0000-00-00:00:00'
            else:
                self.f_date = self.format_date(args.from_date)
            if args.to_date is not None and args.to_date != '-1':
                self.t_date = self.format_date(args.to_date)
            else:
                self.t_date = datetime.now().strftime('%Y-%m-%d:%H:%M')
        else:
            if args.since.endswith('d') or args.since.endswith('D'):
                self.f_date = (datetime.now() + timedelta(days=float('-%s' % (args.since[:-1])))).strftime('%Y-%m-%d:%H:%M')
            elif args.since.endswith('h') or args.since.endswith('H'):
                self.f_date = (datetime.now() + timedelta(days=float('-%s' % round(float(args.since[:-1]) / 24, 2)))).strftime('%Y-%m-%d:%H:%M')
            else:
                self.f_date = (datetime.now() + timedelta(days=float(-args.sincep[:-1]))).strftime('%Y-%m-%d:%H:%M')
            self.t_date = datetime.now().strftime('%Y-%m-%d:%H:%M')

        self.check = True if args.check else False

        decode_result = decode_conf_yml(args)

        if decode_result['decode_error'] is not None:
            raise CtlError(decode_result['decode_error'])

        for key, value in decode_result.items():
            if key == 'decode_error':
                continue
            else:
                self.collect_configure_log(value['list'], value['logs'], collect_dir, key)
        self.thread_run()
        if self.check:
            self.get_total_size()
        else:
            self.generate_tar_ball(run_command_dir, detail_version, time_stamp)
            if self.failed_flag is True:
                info_verbose("The collect log generate at: %s.tar.gz,success %s,fail %s" % (
                    collect_dir, self.success_count, self.fail_count))
                info_verbose(colored("Please check the reason of failed task in log: %s\n" % (
                        self.logger_dir + self.logger_file), 'yellow'))
            else:
                info_verbose("The collect log generate at: %s/collect-log-%s-%s.tar.gz,success %s,fail %s" % (
                    run_command_dir, detail_version, time_stamp, self.success_count, self.fail_count))
            summary_file = collect_dir + 'summary.txt'
            with open(summary_file, 'a+') as f:
                f.write('success:%s,fail:%s' % (self.success_count, self.fail_count) + '\n')
                for key, value in self.fail_list.items():
                    f.write('%s, %s' % (key, value) + '\n')