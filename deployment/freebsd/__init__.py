from os import path
from shutil import rmtree
from tempfile import mkdtemp
from OpenSSL import crypto
from fabric import api as fab
from fabric.contrib.project import rsync_project
from fabric.contrib.files import upload_template
from ezjailremote import fabfile as ezjail
from ezjailremote.utils import jexec

from deployment import ALL_STEPS


def deploy(config, steps=[]):
    print "Deploying on FreeBSD."
    # by default, all steps are performed on the jailhost
    fab.env['host_string'] = config['host']['ip_addr']

    # TODO: step execution should be moved up to general deployment,
    # it's not OS specific (actually, it should move to ezjail-remote eventually)

    all_steps = {
        'bootstrap': (bootstrap, (config,)),
        'create-appserver': (create_appserver, (config,)),
        'configure-appserver': (jexec, (config['appserver']['ip_addr'], configure_appserver, config)),
        'update-appserver': (jexec, (config['appserver']['ip_addr'], update_appserver, config)),
        'create-webserver': (create_webserver, (config,)),
        'configure-webserver': (jexec, (config['webserver']['ip_addr'], configure_webserver, config)),
        'update-webserver': (jexec, (config['webserver']['ip_addr'], update_webserver, config)),
        }

    for step in ALL_STEPS:
        if not steps or step in steps:
            funk, arx = all_steps[step]
            print step
            funk(*arx)


def bootstrap(config):
    # run ezjailremote's basic bootstrap
    orig_user = fab.env['user']
    host_ip = config['host']['ip_addr']
    ezjail.bootstrap(primary_ip=host_ip)
    fab.env['user'] = orig_user

    # configure IP addresses for the jails
    fab.sudo("""echo 'cloned_interfaces="lo1"' >> /etc.rc.conf""")
    fab.sudo("""echo 'ipv4_addrs_lo1="127.0.0.2-10/32"' >> /etc.rc.conf""")
    fab.sudo('ifconfig lo1 create')
    for ip in range(2, 11):
        fab.sudo('ifconfig lo1 alias 127.0.0.%s' % ip)
    for jailhost in ['webserver', 'appserver']:
        alias = config[jailhost]['ip_addr']
        if alias != host_ip and not alias.startswith('127.0.0.'):
            fab.sudo("""echo 'ifconfig_%s_alias="%s"' >> /etc/rc.conf""" % (config['host']['iface'], alias))
            fab.sudo("""ifconfig %s alias %s""" % (config['host']['iface'], alias))

    # set the time
    fab.sudo("cp /usr/share/zoneinfo/%s /etc/localtime" % config['host']['timezone'])
    fab.sudo("ntpdate %s" % config['host']['timeserver'])

    # configure crypto volume for jails
    fab.sudo("""gpart add -t freebsd-zfs -l jails -a8 %s""" % config['host']['root_device'])
    fab.puts("You will need to enter the passphrase for the crypto volume THREE times")
    fab.puts("Once to provide it for encrypting, a second time to confirm it and a third time to mount the volume")
    fab.sudo("""geli init gpt/jails""")
    fab.sudo("""geli attach gpt/jails""")
    fab.sudo("""zpool create jails gpt/jails.eli""")
    fab.sudo("""sudo zfs mount -a""")  # sometimes the newly created pool is not mounted automatically

    # install ezjail
    ezjail.install(source='cvs', jailzfs='jails/ezjail', p=True)


def create_appserver(config):
    ezjail.create('appserver',
        config['appserver']['ip_addr'])


def configure_appserver(config):
    # create application user
    app_user = config['appserver']['app_user']
    app_home = config['appserver']['app_home']
    fab.sudo("pw user add %s" % app_user)
    # upload port configuration
    local_resource_dir = path.join(path.abspath(path.dirname(__file__)))
    fab.sudo("mkdir -p /var/db/ports/")
    fab.put(path.join(local_resource_dir, 'appserver/var/db/ports/*'),
        "/var/db/ports/",
        use_sudo=True)
    # install ports
    for port in ['lang/python',
        'sysutils/py-supervisor',
        'net/rsync',
        'textproc/libxslt']:
        with fab.cd('/usr/ports/%s' % port):
            fab.sudo('make install')
    fab.sudo('mkdir -p %s' % app_home)
    fab.sudo('''echo 'supervisord_enable="YES"' >> /etc/rc.conf ''')
    local_resource_dir = path.join(path.abspath(path.dirname(__file__)))
    # configure supervisor (make sure logging is off!)
    upload_template(filename=path.join(local_resource_dir, 'supervisord.conf.in'),
        context=dict(app_home=app_home, app_user=app_user),
        destination='/usr/local/etc/supervisord.conf',
        backup=False,
        use_sudo=True)
    config['appserver']['configure-hasrun'] = True


def update_appserver(config):
    configure_hasrun = config['appserver'].get('configure-hasrun', False)
    # upload sources
    import briefkasten
    from deployment import APP_SRC
    app_home = config['appserver']['app_home']
    app_user = config['appserver']['app_user']
    base_path = path.abspath(path.join(path.dirname(briefkasten.__file__), '..'))
    local_paths = ' '.join([path.join(base_path, app_path) for app_path in APP_SRC])
    fab.sudo('chown -R %s %s' % (fab.env['user'], app_home))
    rsync_project(app_home, local_paths, delete=True)
    # upload theme
    fs_remote_theme = path.join(app_home, 'themes')
    config['appserver']['fs_remote_theme'] = path.join(fs_remote_theme, path.split(config['appserver']['fs_theme_path'])[-1])
    fab.run('mkdir -p %s' % fs_remote_theme)
    rsync_project(fs_remote_theme,
        path.abspath(path.join(config['fs_path'], config['appserver']['fs_theme_path'])),
        delete=True)
    # create custom buildout.cfg
    local_resource_dir = path.join(path.abspath(path.dirname(__file__)))
    upload_template(filename=path.join(local_resource_dir, 'buildout.cfg.in'),
        context=config['appserver'],
        destination=path.join(app_home, 'buildout.cfg'),
        backup=False)

    fab.sudo('chown -R %s %s' % (app_user, app_home))
    # bootstrap and run buildout
    with fab.cd(app_home):
        if configure_hasrun:
            fab.sudo('python2.7 bootstrap.py', user=app_user)
        fab.sudo('bin/buildout', user=app_user)
    # start supervisor
    if configure_hasrun:
        fab.sudo('/usr/local/etc/rc.d/supervisord start')
    else:
        fab.sudo('supervisorctl restart briefkasten')


def create_webserver(config):
    ezjail.create('webserver',
        config['webserver']['ip_addr'])


def configure_webserver(config):
    # create www user
    fab.sudo("pw user add %s" % config['webserver']['wwwuser'])
    # upload port configuration
    local_resource_dir = path.join(path.abspath(path.dirname(__file__)))
    fab.sudo("mkdir -p /var/db/ports/")
    fab.put(path.join(local_resource_dir, 'webserver/var/db/ports/*'),
        "/var/db/ports/",
        use_sudo=True)
    # install ports
    for port in ['www/nginx', ]:
        with fab.cd('/usr/ports/%s' % port):
            fab.sudo('make install')
    fab.sudo('''echo 'nginx_enable="YES"' >> /etc/rc.conf ''')
    # create or upload pem
    cert_file = config['webserver']['cert_file']
    key_file = config['webserver']['key_file']
    tempdir = None

    # if no files were given, create an ad-hoc certificate and key
    if not (path.exists(cert_file)
        or path.exists(key_file)):

        tempdir = mkdtemp()
        cert_file = path.join(tempdir, 'briefkasten.crt')
        key_file = path.join(tempdir, 'briefkasten.key')

        # create a key pair
        # based on http://skippylovesmalorie.wordpress.com/2010/02/12/how-to-generate-a-self-signed-certificate-using-pyopenssl/
        pkey = crypto.PKey()
        pkey.generate_key(crypto.TYPE_RSA, 1024)

        # create a minimal self-signed cert
        cert = crypto.X509()
        cert.get_subject().CN = config['webserver']['fqdn']
        cert.set_serial_number(1000)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(365 * 24 * 60 * 60)
        cert.set_issuer(cert.get_subject())
        cert.set_pubkey(pkey)
        cert.sign(pkey, 'sha1')
        open(cert_file, "wt").write(
            crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        open(key_file, "wt").write(
            crypto.dump_privatekey(crypto.FILETYPE_PEM, pkey))
    fab.put(cert_file, '/usr/local/etc/nginx/briefkasten.crt', use_sudo=True)
    fab.put(key_file, '/usr/local/etc/nginx/briefkasten.key', use_sudo=True)
    if tempdir:
        rmtree(tempdir)
    config['webserver']['configure-hasrun'] = True


def update_webserver(config):
    local_resource_dir = path.join(path.abspath(path.dirname(__file__)))
    # configure nginx (make sure logging is off!)
    upload_template(filename=path.join(local_resource_dir, 'nginx.conf.in'),
        context=dict(
            fqdn=config['webserver']['fqdn'],
            app_ip=config['appserver']['ip_addr'],
            app_port=config['appserver']['port'],
            wwwuser=config['webserver']['wwwuser']),
        destination='/usr/local/etc/nginx/nginx.conf',
        backup=False,
        use_sudo=True)
    # start nginx
    if config['webserver'].get('configure-hasrun', False):
        fab.sudo('/usr/local/etc/rc.d/nginx start')
    else:
        fab.sudo('/usr/local/etc/rc.d/nginx reload')