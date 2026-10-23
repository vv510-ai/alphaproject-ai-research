import { useState } from 'react';
import type { FormEvent } from 'react';
import type { WatchGroup } from './api';

export default function WatchGroups({ groups, busy, error, onCreate, onRename, onDelete }: {
  groups: WatchGroup[]; busy: boolean; error: string;
  onCreate: (name: string) => Promise<boolean>; onRename: (group: WatchGroup, name: string) => Promise<boolean>;
  onDelete: (group: WatchGroup) => Promise<boolean>;
}) {
  const [name, setName] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [confirm, setConfirm] = useState<string | null>(null);
  async function create(event: FormEvent) { event.preventDefault(); if (await onCreate(name.trim())) setName(''); }
  return <details className="watch-groups"><summary>管理自选分组<span>{groups.length} 个分组</span></summary>
    <p className="field-hint">分组和持仓标记会在不同设备间同步。持仓标记只用于整理列表。</p>
    <form onSubmit={event => void create(event)} className="group-form"><label htmlFor="new-group">新分组名称<input id="new-group" name="new-group" value={name} onChange={event => setName(event.target.value)} placeholder="例如 长期关注" maxLength={20} disabled={busy} /></label><button type="submit" className="button button-outline" disabled={busy}>创建分组</button></form>
    {error && <p className="research-warning" role="alert">{error}</p>}
    <ul className="group-list">{groups.map(group => <li key={group.id}>
      {editing === group.id ? <form className="group-form" onSubmit={event => { event.preventDefault(); void onRename(group, draft.trim()).then(ok => { if (ok) setEditing(null); }); }}><label htmlFor={`rename-${group.id}`}>修改分组名称<input id={`rename-${group.id}`} name="group-name" value={draft} onChange={event => setDraft(event.target.value)} maxLength={20} disabled={busy} /></label><button className="button button-outline" disabled={busy} type="submit">保存名称</button><button className="button button-quiet" type="button" disabled={busy} onClick={() => setEditing(null)}>取消</button></form> : <><strong>{group.name}</strong><div className="group-actions"><button className="button button-quiet" disabled={busy} onClick={() => { setEditing(group.id); setDraft(group.name); setConfirm(null); }} aria-label={`重命名${group.name}分组`}>重命名</button><button className="button button-quiet" disabled={busy} onClick={() => setConfirm(group.id)} aria-label={`删除${group.name}分组`}>删除</button></div></>}
      {confirm === group.id && <div className="group-confirm"><p>删除“{group.name}”后，组内股票会移到未分组。</p><button className="button button-danger-small" disabled={busy} onClick={() => void onDelete(group).then(ok => { if (ok) setConfirm(null); })}>删除分组并保留股票</button><button className="button button-quiet" disabled={busy} onClick={() => setConfirm(null)}>取消</button></div>}
    </li>)}</ul>
  </details>;
}
