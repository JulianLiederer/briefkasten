from os import listdir, mkdir
from os.path import join
from pytest import fixture
import shutil


def test_cleanup_deletes_message(dropbox_container, dropbox):
    assert 'message' in listdir(dropbox.fs_path)
    dropbox.cleanup()
    assert 'message' not in listdir(dropbox.fs_path)


def test_cleanup_deletes_dirty_attachments(dropbox_container, dropbox):
    assert 'attach' in listdir(dropbox.fs_path)
    dropbox.cleanup()
    assert 'attach' not in listdir(dropbox.fs_path)


def test_initial_backup_creation(dropbox_container, dropbox):
    assert 'dirty.zip.pgp' not in listdir(dropbox.fs_path)
    dropbox._create_backup()
    assert 'dirty.zip.pgp' in listdir(dropbox.fs_path)


def test_initial_backup_removed_on_cleanup(dropbox_container, dropbox):
    dropbox._create_backup()
    assert 'dirty.zip.pgp' in listdir(dropbox.fs_path)
    dropbox.cleanup()
    assert 'dirty.zip.pgp' not in listdir(dropbox.fs_path)


@fixture
def cleansed_file_src(testing):
    return testing.asset_path('attachment.txt')


@fixture
def cleansed_dropbox(dropbox_container, dropbox, cleansed_file_src):
    mkdir(join(dropbox.fs_path, 'clean'))
    shutil.copy2(cleansed_file_src, join(dropbox.fs_path, 'clean'))
    return dropbox


def test_cleanup_deletes_cleansed_attachments(dropbox_container, cleansed_dropbox):
    assert 'clean' in listdir(cleansed_dropbox.fs_path)
    cleansed_dropbox.cleanup()
    assert 'clean' not in listdir(cleansed_dropbox.fs_path)


def test_fs_cleansed_attachments_empty(dropbox_container, dropbox):
    assert dropbox.fs_cleansed_attachments == []


def test_fs_cleansed_attachments(dropbox_container, cleansed_dropbox, cleansed_file_src):
    fs_cleansed = cleansed_dropbox.fs_cleansed_attachments[0]
    assert open(fs_cleansed, 'r').readlines() == open(cleansed_file_src, 'r').readlines()


def test_create_archive(dropbox_container, cleansed_dropbox):
    assert listdir(dropbox_container.fs_archive_cleansed) == []
    cleansed_dropbox._create_archive()
    assert listdir(dropbox_container.fs_archive_cleansed) == ['%s.zip.pgp' % cleansed_dropbox.drop_id]
