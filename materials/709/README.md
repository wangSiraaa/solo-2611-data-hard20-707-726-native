# 合成兑换历史
`history.sqlite`是可直接读取的合成历史库，不含兑换服务实现。`ledger_reader.py`是读取现有余额、成功兑换和撤销事实的客户。运行`python3 ledger_reader.py history.sqlite`；实现服务时复制该库到工作数据目录再推进operations.jsonl，不修改材料原件。原表和已有列的读取含义需保持，可加列/表或兼容视图。

历史O_OLD原价1200，按v1扣I_FOOD/I_SHIP各1次，优惠200+100，实付900；库中余额各1次已经是扣后事实，不能导入时再扣。r2将食品券门槛改1500，O_NEW原价仍1200，资格失败，两余额不变；取消O_OLD仍各返1次至2，重复取消不再返。I_FOOD+I_ALT同属FOOD，应组合拒绝。

request_results保存首次请求的原内容和结果；R_OLD的request_json即redemptions中的原请求。相同request_id复读冻结结果，新request_id才是新尝试。JSON对象键序不影响内容，行顺序和券列表顺序保留。拒绝结果也可持久保存，但不得伪造成功兑换或扣次。
