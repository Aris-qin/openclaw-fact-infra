"""Regenerate from v3.py passed as argv[1]. Only opens an in-memory SQLite DB."""
import sys,re,json,sqlite3,pathlib
source=pathlib.Path(sys.argv[1]).read_text()
ddl=re.search(r'SCHEMA = """(.*?)"""',source,re.S)[1]
out=pathlib.Path(__file__).parent/'fixtures'
(out/'schema.sql').write_text(ddl)
c=sqlite3.connect(':memory:');c.executescript(ddl)
names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence' ORDER BY name")]
result={t:{'columns':list(c.execute(f'PRAGMA table_info({t})')),'foreignKeys':list(c.execute(f'PRAGMA foreign_key_list({t})')),'indexes':{r[1]:{'info':list(c.execute(f'PRAGMA index_info({r[1]})')),'unique':r[2],'origin':r[3],'partial':r[4]} for r in c.execute(f'PRAGMA index_list({t})')}} for t in names}
result['triggers']=list(c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"))
(out/'schema.json').write_text(json.dumps(result))
print(f'{len(names)} tables; {sum(len(result[t]["columns"]) for t in names)} fields')
