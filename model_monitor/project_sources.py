"""Read explicit desktop project assignments without changing Codex state."""
import json
from pathlib import Path


def desktop_bindings(path,projects):
    path=Path(path)
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:return {},[]
    except OSError:return {},[('project_state_read_failure','desktop-project-state')]
    except (ValueError,UnicodeError):return {},[('project_state_invalid','desktop-project-state')]
    if not isinstance(data,dict):return {},[('project_state_invalid','desktop-project-state')]
    assignments=data.get('thread-project-assignments',{})
    hosts=data.get('thread-project-membership-host-ids',{})
    migrations=data.get('app-server-project-id-by-legacy-project-id-by-host',{})
    projectless=data.get('projectless-thread-ids',[])
    if not all(isinstance(v,dict) for v in (assignments,hosts,migrations)) or not isinstance(projectless,list) or any(not isinstance(t,str) for t in projectless):
        return {},[('project_state_invalid','desktop-project-state')]
    mapping=migrations.get('local:'+str(path.parent.resolve()),{})
    if not isinstance(mapping,dict):return {},[('project_state_invalid','desktop-project-state/migration')]
    bindings={};issues=[]
    for tid,assignment in assignments.items():
        source='desktop-project-state/thread-project-assignments/'+tid
        # Older local bindings may omit membership; the exact local migration still proves host.
        # 旧本地绑定可能缺少 membership，精确的本地主机迁移映射仍提供来源。
        if not isinstance(assignment,dict) or assignment.get('projectKind')!='local' or (tid in hosts and hosts[tid]!='local'):
            issues.append(('project_binding_invalid',source));continue
        legacy=assignment.get('projectId')
        canonical=mapping.get(legacy) if isinstance(legacy,str) else None
        if not isinstance(canonical,str) or canonical not in projects:
            issues.append(('project_binding_invalid',source));continue
        # Stable ID migration joins the desktop assignment to the database project.
        # 用稳定 ID 迁移映射关联桌面分配和数据库项目，不按名字猜测。
        bindings[tid]={'project_id':canonical,'source':source+' -> local migration '+legacy+' -> '+canonical}
    for tid in projectless:
        if tid in bindings:
            issues.append(('project_binding_conflict','desktop-project-state/'+tid))
            bindings[tid]={'project_id':None,'source':'桌面项目分配与明确无项目标记冲突；归属待确认'}
        else:bindings[tid]={'project_id':None,'source':'desktop-project-state/projectless-thread-ids（明确无项目）'}
    return bindings,issues
