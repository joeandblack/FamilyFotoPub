import re
import pytest
from app.imports.ignore import should_ignore, IGNORE_SQL_PATTERN


@pytest.mark.parametrize('path',[r'F:\photos\_vti_cnf\photo.jpg','album/_VTI_CNF/nested/photo.JPG','_vti_cnf/photo.jpg'])
def test_website_metadata_directory_is_ignored(path):
    assert should_ignore(path)
    assert re.search(IGNORE_SQL_PATTERN,path)


@pytest.mark.parametrize('path',['album/_vti_cnf.jpg','album/_vti_cnf_backup/photo.jpg','album/photo.jpg'])
def test_website_metadata_rule_does_not_hide_real_images(path):
    assert not should_ignore(path)
    assert not re.search(IGNORE_SQL_PATTERN,path)


def test_website_metadata_uploads_are_filtered():
    from app.uploads import manifest, DEFAULTS
    _, files, *_ = manifest({'files':[
        {'path':'Album/_VTI_CNF/photo.jpg','size':256},
        {'path':'Album/photo.jpg','size':1000}]}, DEFAULTS)
    assert [f['disposition'] for f in files] == ['ignored', 'accepted']


@pytest.mark.parametrize('path',[
    'clip.moff','clip.MODD','clip.m2ts.modd','._clip.moff','.DS_Store','photo.xmp',r'F:\PHOTOS\sub\photo.XMP','document.PAGES','sheet.numbers',r'F:\PHOTOS\sub\install.DMG',
    'F:/PHOTOS/sub/.ds_store',r'F:\PHOTOS\sub\.DS_Store',
    'clip.THM','clip.thm','._Thumbs.db','._desktop.ini','._.DS_Store','clip.LRF','subtitle.srt','F:/PHOTOS/sub/clip.lrf',r'F:\PHOTOS\sub\subtitle.SRT',
    'photo.AAE','Thumbs.db','other.DB','file.info','page.htm','page.HTML',
    'desktop.ini','config.INI',r'F:\PHOTOS\sub\desktop.ini',
    'disc.VCD','notes.txt','process.LOG','backup.BUP','VIDEO_TS.IFO',
    'archive.ZIP','archive.tar.gz','audio.MP2','disc.DVD','session.ses','subtitle.SPU',
    r'F:\PHOTOS\sub\archive.zip','F:/PHOTOS/sub/subtitle.spu',
    r'F:\PHOTOS\sub\notes.TXT','F:/PHOTOS/sub/VIDEO_TS.ifo',
    r'F:\PHOTOS\sub\photo.aae','F:/PHOTOS/sub/Thumbs.db',
    r'F:\PHOTOS\sub\file.INFO','F:/PHOTOS/sub/page.HTM',
    '.DS_Store.jpg','.hidden.jpg','F:/PHOTOS/.hidden.jpg',r'F:\PHOTOS\._video.MOV'])
def test_ignore_names_and_sql_equivalent(path):
    assert should_ignore(path)
    assert re.search(IGNORE_SQL_PATTERN,path)


@pytest.mark.parametrize('path',['photo.jpg','clip.modd.jpg',
    'F:/album.moff/photo.jpg','clip.moff.backup','clip.thm.jpg','photo.xmp.jpg','F:/album.xmp/photo.jpg',
    'photo.aae.jpg','file.db.backup','file.info.jpg','page.html.jpg','file.ini.jpg','F:/folder.ini/photo.jpg',
    'disc.vcd.jpg','notes.txt.jpg','process.log.jpg','backup.bup.jpg','VIDEO_TS.ifo.jpg','F:/folder.txt/photo.jpg',
    'archive.zip.jpg','archive.gz.jpg','audio.mp2.jpg','disc.dvd.jpg','session.ses.jpg','subtitle.spu.jpg','F:/folder.zip/photo.jpg',
    'F:/album.db/photo.jpg',r'F:\album.info\photo.jpg','clip.lrf.jpg','subtitle.srt.jpg','F:/folder.srt/photo.jpg',
    'F:/PHOTOS/.album/photo.jpg',r'F:\PHOTOS\.album\photo.jpg'])
def test_media_and_similar_names_are_not_ignored(path):
    assert not should_ignore(path)
    assert not re.search(IGNORE_SQL_PATTERN,path)


def test_inventory_skips_ignored_files_before_stat(tmp_path,monkeypatch):
    from app.imports import importer
    from pathlib import Path
    for name in ('README','photo.ithmb','photo.JPGZ','photo.avif','photo.jpg','file.ASF','file.comicdoc','file.Doc','file.DOCX','file.isf','file.LNK','file.cb','file.CNB','clip.modd','.DS_Store','clip.THM','._Thumbs.db','._desktop.ini','._.DS_Store','photo.AAE','Thumbs.db','other.DB','file.info','page.htm','page.HTML','desktop.ini','config.INI','disc.VCD','notes.txt','process.LOG','backup.BUP','VIDEO_TS.IFO','archive.zip','archive.gz','audio.MP2','disc.dvd','session.ses','subtitle.spu'):
        (tmp_path/name).write_bytes(b'fixture')
    (tmp_path/'_VTI_CNF').mkdir()
    (tmp_path/'_VTI_CNF'/'fake.jpg').write_bytes(b'website metadata')
    stat=Path.stat
    def guarded_stat(path,*args,**kwargs):
        if path.parent==tmp_path:assert not should_ignore(path), 'Ignored source must not be stat/read'
        return stat(path,*args,**kwargs)
    monkeypatch.setattr(Path,'stat',guarded_stat)
    monkeypatch.setattr(importer,'publish',lambda *args:None)
    class DB:
        records=[]
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def executemany(self,query,rows): self.records.extend(rows)
        def execute(self,*args): pass
        def commit(self): pass
    class Monitor:
        def check(self): pass
    db=DB()
    importer.inventory(db,1,tmp_path,Monitor())
    assert [Path(row[1]).name for row in db.records]==['photo.jpg']


@pytest.mark.parametrize('extension',['asf','comicdoc','doc','docx','isf','lnk','cb','cnb','bak','kmproject','vcf','prin','prproj'])
def test_new_extensions_match_only_filename_suffix(extension):
    for path in (f'file.{extension}',f'F:/PHOTOS/file.{extension.upper()}',rf'F:\PHOTOS\file.{extension.title()}'):
        assert should_ignore(path)
        assert re.search(IGNORE_SQL_PATTERN,path)
    for path in (f'file.{extension}.jpg',f'F:/album.{extension}/photo.jpg'):
        assert not should_ignore(path)
        assert not re.search(IGNORE_SQL_PATTERN,path)


@pytest.mark.parametrize('name',['README','folder.with.dot/README',r'F:\nested.folder\README','trailing.'])
def test_explicit_extensionless_rule(name):
    assert should_ignore(name)
    assert re.search(IGNORE_SQL_PATTERN,name)


@pytest.mark.parametrize('name',['photo.jpeg.sb-e9757e01-SaUZZs','notes.csv','settings.json','document.rtf'])
def test_recognized_nonmedia_and_temporary_saves(name):
    assert should_ignore(name) and re.search(IGNORE_SQL_PATTERN,name)


@pytest.mark.parametrize('name',['F:/media/manual.pdf','photo.PDF','photo.psd','photo.psb','unknown.xyz','video.ts','photo.bak.jpg','photo.jpeg.sb-token-id.jpg'])
def test_unknown_and_media_formats_stay_reviewable(name):
    assert not should_ignore(name) and not re.search(IGNORE_SQL_PATTERN,name)


def test_import15_ignores_and_remote_manifest():
    from app.imports.ignore import IMPORT15_EXTENSIONS
    from app.uploads import manifest, DEFAULTS
    names=['file.'+e.upper() for e in IMPORT15_EXTENSIONS]+['README']
    for name in names:
        assert should_ignore(name) and re.search(IGNORE_SQL_PATTERN,name)
    for name in ['file.psd','file.PSD','photo.jpg','movie.mp4']:
        assert not should_ignore(name) and not re.search(IGNORE_SQL_PATTERN,name)
    _,files,*_=manifest({'files':[{'path':'Album/'+n,'size':1} for n in names+['file.PSD','photo.jpg']]},DEFAULTS)
    assert [f['disposition'] for f in files]==['ignored']*len(names)+['unsupported','accepted']


@pytest.mark.skipif(__import__('os').environ.get('FAMILYFOTO_INTEGRATION')!='1',reason='Local PostgreSQL')
def test_postgres_ignore_parity():
    from app.database import connection
    from app.imports.ignore import IMPORT15_EXTENSIONS
    names=['Album/file.'+e.upper() for e in IMPORT15_EXTENSIONS]+['Album/README','file.PSD','photo.jpg','folder.jpg/README','folder.avif/photo.jpg','trailing.','photo.jpeg.sb-e9757e01-SaUZZs','backup.BAK','edit.KMPROJECT','contacts.VCF','job.PRIN','edit.PRPROJ','manual.pdf','notes.csv','document.rtf']
    with connection() as db:
        names += ['Album/_vti_cnf/photo.jpg', r'F:\Album\_VTI_CNF\nested\photo.jpg', 'Album/_vti_cnf_backup/photo.jpg']
        for name in names:
            assert db.execute('SELECT %s ~ %s matches',(name,IGNORE_SQL_PATTERN)).fetchone()['matches']==should_ignore(name)
