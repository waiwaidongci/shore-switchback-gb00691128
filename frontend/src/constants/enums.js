export const roles = {analyst: '分析员', reviewer: '复核员', viewer: '只读访客'};
export const states = {queued: '排队', running: '计算中', completed: '已计算', failed: '需修正'};
export const decisions = {approved: '已批准', returned: '已退回'};
export const kinds = {base: '原始数据版本', merged: '补报合并版本', supplement: '补报文件'};
export const actions = {import:'导入数据',supplement:'补报窗口',supplement_failed:'补报未通过',merge:'合并窗口版本',queue:'提交计算',completed:'完成计算',failed:'计算失败',activate:'启用新版本',approved:'批准发布',returned:'退回结果',schedule:'建立计划',cancel:'停用计划',delivered:'报告入箱'};
