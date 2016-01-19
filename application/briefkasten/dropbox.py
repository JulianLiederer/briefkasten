# -*- coding: utf-8 -*-
import gnupg
import shutil
import tarfile
import yaml
from cStringIO import StringIO as BIO
from glob import glob
from humanfriendly import parse_size
from jinja2 import Environment, PackageLoader
from json import load, dumps
from os import makedirs, mkdir, chmod, environ, listdir, remove, stat
from os.path import exists, isdir, isfile, join, splitext
from pyramid.settings import asbool, aslist
from random import SystemRandom
from subprocess import call

from .notifications import (
    checkRecipient,
    sendMultiPart,
    setup_smtp_factory
)

jinja_env = Environment(loader=PackageLoader('briefkasten', 'templates'))
allchars = '23456qwertasdfgzxcvbQWERTASDFGZXCVB789yuiophjknmYUIPHJKLNM'


def generate_drop_id(length=8):
    rng = SystemRandom()
    drop_id = ""
    for i in range(length):
        drop_id += rng.choice(allchars)
    return drop_id


def sanitize_filename(filename):
    """preserve the file ending, but replace the name with a random token """
    # TODO: fix broken splitext (it reveals everything of the filename after the first `.` - doh!)
    token = generate_drop_id()
    name, extension = splitext(filename)
    if extension:
        return '%s%s' % (token, extension)
    else:
        return token


class DropboxContainer(object):

    def __init__(self, root=None, settings=None):
        self.fs_root = root
        self.fs_path = join(root, 'drops')
        self.fs_submission_queue = join(root, 'submissions')
        self.fs_scratchdir = join(root, 'scratchdir')

        # ensure directories exist
        for directory in [self.fs_root, self.fs_path, self.fs_submission_queue]:
            if not exists(directory):
                makedirs(directory)

        # initialise settings from disk and parameters
        # settings provided as init parameter take precedence over values on-disk
        # which in turn take precedence over default values
        self.settings = dict(
            attachment_size_threshold=u'2Mb',
        )

        self.settings.update(**self.parse_settings())
        if settings is not None:
            self.settings.update(**settings)

        # set smtp instance defensively, to not overwrite mocked version from test settings:
        if 'smtp' not in self.settings:
            self.settings['smtp'] = setup_smtp_factory(**self.settings)

        # setup GPG
        self.gpg_context = gnupg.GPG(gnupghome=self.settings['fs_pgp_pubkeys'])

        # convert human readable size to bytes
        self.settings['attachment_size_threshold'] = parse_size(self.settings['attachment_size_threshold'])

    def parse_settings(self):
        fs_settings = join(self.fs_root, 'settings.yaml')
        if exists(fs_settings):
            with open(fs_settings, 'r') as settings:
                return yaml.load(settings)
        else:
            return dict()

    def add_dropbox(self, drop_id, message=None, attachments=None):
        return Dropbox(self, drop_id, message=message, attachments=attachments)

    def get_dropbox(self, drop_id):
        """ returns the dropbox with the given id, if it does not exist an empty dropbox
        will be created and returned"""
        return Dropbox(self, drop_id=drop_id)

    def destroy(self):
        shutil.rmtree(self.fs_root)

    def __contains__(self, drop_id):
        return exists(join(self.fs_path, drop_id))

    def __iter__(self):
        for candidate in listdir(self.fs_path):
            if isdir(join(self.fs_path, candidate)):
                yield self.get_dropbox(candidate)


class Dropbox(object):

    def __init__(self, container, drop_id, message=None, attachments=None):
        """
        the attachments are expected to conform to what the webob library uses for file uploads,
        namely an instance of `cgi.FieldStorage` with the following attributes:
            - a file handle under the key `file`
            - the name of the file under `filename`
        """
        self.drop_id = drop_id
        self.container = container
        self.paths_created = []
        self.fs_path = fs_dropbox_path = join(container.fs_path, drop_id)
        self.fs_replies_path = join(self.fs_path, 'replies')
        self.gpg_context = self.container.gpg_context
        self.editors = aslist(self.settings['editors'])
        self.admins = aslist(self.settings['admins'])

        if not exists(fs_dropbox_path):
            mkdir(fs_dropbox_path)
            chmod(fs_dropbox_path, 0770)
            self.paths_created.append(fs_dropbox_path)
            self.status = u'010 created'
            # create an editor token
            self.editor_token = editor_token = generate_drop_id()
            self._write_message(fs_dropbox_path, 'editor_token', editor_token)
        else:
            self.editor_token = open(join(self.fs_path, 'editor_token')).readline()

        if message is not None:
            # write the message into a file
            self._write_message(fs_dropbox_path, 'message', message)
            self.message = message

        # write the attachment into a file
        if attachments is not None:
            for attachment in attachments:
                if attachment is None:
                    continue
                self.add_attachment(attachment)

    @property
    def settings(self):
        return self.container.settings

    @property
    def fs_attachment_container(self):
        return join(self.fs_path, 'attach')

    def update_message(self, newtext):
        """ overwrite the message text. this also updates the corresponding file. """
        self._write_message(self.fs_path, 'message', newtext)

    def add_attachment(self, attachment):
        fs_attachment_container = self.fs_attachment_container
        if not exists(fs_attachment_container):
            mkdir(fs_attachment_container)
            chmod(fs_attachment_container, 0770)
            self.paths_created.append(fs_attachment_container)
        sanitized_filename = sanitize_filename(attachment.filename)
        fs_attachment_path = join(fs_attachment_container, sanitized_filename)
        with open(fs_attachment_path, 'w') as fs_attachment:
            shutil.copyfileobj(attachment.file, fs_attachment)
        fs_attachment.close()
        chmod(fs_attachment_path, 0660)
        self.paths_created.append(fs_attachment_path)
        return sanitized_filename

    def _create_backup(self):
        backup_recipients = [r for r in self.editors + self.admins if checkRecipient(self.gpg_context, r)]

        # this will be handled by watchdog, no need to send for each drop
        if not backup_recipients:
            self.status = u'500 no valid keys at all'
            return self.status

        if asbool(self.settings.get('debug', False)):
            file_out = BIO()
            with tarfile.open(mode='w|', fileobj=file_out) as tar:
                tar.add(join(self.fs_path, 'message'))
                if exists(join(self.fs_path, 'attach')):
                    tar.add(join(self.fs_path, 'attach'))
            self.gpg_context.encrypt(
                file_out.getvalue(),
                backup_recipients,
                always_trust=True,
                output=join(self.fs_path, 'backup.tar.pgp')
            )

    def _notify_editors(self):
        attachments_cleaned = []
        cleaned = join(self.fs_path, 'clean')
        if exists(cleaned):
            attachments_cleaned = [join(cleaned, f) for f in listdir(cleaned) if isfile(join(cleaned, f))]
        return sendMultiPart(
            self.settings['smtp'],
            self.gpg_context,
            self.settings['mail.default_sender'],
            self.editors,
            u'Drop %s' % self.drop_id,
            self._notification_text,
            attachments_cleaned
        )

    def _process_attachments(self, testing):
        fs_process = join(self.settings['fs_bin_path'], 'process-attachments.sh')
        fs_config = join(
            self.settings['fs_bin_path'],
            'briefkasten%s.conf' % ('_test' if testing else ''))
        shellenv = environ.copy()
        shellenv['PATH'] = '%s:%s:/usr/local/bin/:/usr/local/sbin/' % (shellenv['PATH'], self.settings['fs_bin_path'])
        call(
            "%s -d %s -c %s" % (fs_process, self.fs_path, fs_config),
            shell=True,
            env=shellenv)

    def process(self, purge_meta_data=True, testing=False):
        """ Calls the external cleanser scripts to (optionally) purge the meta data and then
            send the contents of the dropbox via email.
        """

        if self.num_attachments > 0:
            self.status = u'100 processor running'
            self._create_backup()
            self._process_attachments(testing=testing)

        try:
            if self._notify_editors() > 0:
                self.status = '900 success'
            else:
                self.status = '505 smtp failure'
        except Exception as exc:
            self.status = '510 smtp error (%s)' % exc

        self.cleanup()
        return self.status

    def add_reply(self, reply):
        """ Add an editorial reply to the drop box.

            :param reply: the message, must conform to  :class:`views.DropboxReplySchema`

        """
        self._write_message(self.fs_replies_path, 'message_001.txt', dumps(reply))

    def _write_message(self, fs_container, fs_name, message):
        if not exists(fs_container):
            mkdir(fs_container)
            chmod(fs_container, 0770)
        fs_reply_path = join(fs_container, fs_name)
        with open(fs_reply_path, 'w') as fs_reply:
            fs_reply.write(message.encode('utf-8'))
        chmod(fs_reply_path, 0660)
        self.paths_created.append(fs_reply_path)

    @property
    def _notification_text(self):
        return jinja_env.get_template('editor_email.pt').render(
            num_attachments=self.num_attachments,
            dropbox=self)

    @property
    def num_attachments(self):
        """returns the current number of uploaded attachments in the filesystem"""
        if exists(self.fs_attachment_container):
            return len(listdir(self.fs_attachment_container))
        else:
            return 0

    @property
    def size_attachments(self):
        """returns the number of bytes that the attachments take up on disk"""
        total_size = 0
        if exists(self.fs_attachment_container):
            for attachment in glob('%s/*.*' % self.fs_attachment_container):
                total_size += stat(attachment).st_size
        return total_size

    @property
    def replies(self):
        """ returns a list of strings """
        fs_reply_path = join(self.fs_replies_path, 'message_001.txt')
        if exists(fs_reply_path):
            return [load(open(fs_reply_path, 'r'))]
        else:
            return []

    @property
    def status(self):
        """ returns either 'created', 'quarantined', 'success' or 'failure'
        """
        try:
            with open(join(self.fs_path, u'status')) as status_file:
                return status_file.readline()
        except IOError:
            return u'000 no status file'

    @property
    def status_int(self):
        """ returns the status as integer, so it can be used in comparisons"""
        return int(self.status.split()[0])

    @status.setter
    def status(self, state):
        with open(join(self.fs_path, u'status'), 'w') as status_file:
            status_file.write(state)

    def cleanup(self):
        """ ensures that no data leaks from drop after processing """
        if self.status_int >= 500:
            self.wipe()
        else:
            self.sanitize()

    def sanitize(self):
        """ removes all unencrypted user input """
        shutil.rmtree(join(self.fs_path, u'attach'), ignore_errors=True)
        try:
            remove(join(self.fs_path, u'message'))
            remove(join(self.fs_path, u'backup.tar.pgp'))
        except OSError:
            pass

    def wipe(self):
        """ removes all data except the status file"""
        self.sanitize()
        shutil.rmtree(join(self.fs_path, u'clean'), ignore_errors=True)
        try:
            remove(join(self.fs_path, u'backup.tar.pgp'))
        except OSError:
            pass

    def __repr__(self):
        return u'Dropbox %s (%s) at %s' % (
            self.drop_id,
            self.status,
            self.fs_path,
        )

    @property
    def drop_url(self):
        return self.settings['dropbox_view_url_format'] % self.drop_id

    @property
    def editor_url(self):
        return self.settings['dropbox_editor_url_format'] % (
            self.drop_id,
            self.editor_token)
