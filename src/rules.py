from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='企业排污许可与超标处置'; ENTITY='排污事件'; ID_PREFIX='ED'
SEVERITIES=['normal', 'watch', 'exceedance', 'major']; STATES=['reported', 'assessing', 'remediation', 'inspection', 'closed']; TRANSITIONS={'reported': ['assessing'], 'assessing': ['remediation'], 'remediation': ['inspection'], 'inspection': ['closed'], 'closed': []}; TRANSITION_ROLES={'assessing': ['compliance_officer'], 'remediation': ['operator'], 'inspection': ['compliance_officer'], 'closed': ['director']}
CREATE_ROLES=set(['operator', 'compliance_officer']); RECORD_ROLES=set(['operator', 'compliance_officer']); AUDIT_ROLES=set(['director', 'viewer']); VIEW_ROLES=set(['operator', 'compliance_officer', 'director', 'viewer'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'exceedance': 6.0, 'major': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'exceedance': 8, 'major': 4}; TERMINAL_STATES=set(['closed'])
REVIEW_STATE='inspection'; REVIEW_CONFIRM_ROLES=set(['compliance_officer'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def is_review_state(status): return status==REVIEW_STATE
def review_blockers(status,open_records,current_record_ids,current_version,confirmed_record_ids,confirmed_version):
    """复查关闭阻塞项：仅在关闭前的复查阶段评估；确认快照必须覆盖当前全部材料与事件版本。"""
    blockers=[]
    if status==REVIEW_STATE:
        if open_records>0: blockers.append("仍有未关闭事项")
        if confirmed_record_ids is None: blockers.append("复查材料尚未经合规员确认，请先完成复查确认")
        else:
            if list(confirmed_record_ids)!=list(current_record_ids): blockers.append("复查确认后新增了材料，请重新确认")
            if confirmed_version!=current_version: blockers.append("复查确认基于旧的事件版本，请重新确认")
    return blockers
def same_confirmation_snapshot(current_record_ids,current_version,confirmed_record_ids,confirmed_version):
    return confirmed_record_ids is not None and list(confirmed_record_ids)==list(current_record_ids) and confirmed_version==current_version
