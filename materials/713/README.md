# 保留完整槽身份的销售历史
history.sqlite中的SALE1售两套、SALE2售一套，均为P/v1；配置及全部额度身份已明确，无须从SKU合计推回槽位。SALE1的LEFT售X两件，其中RET_OLD已退一件；RIGHT售X四件，未退。旧记录是已生效历史，不应重售或重退。

运行`python3 credit_reader.py history.sqlite`；实现服务时复制库，推进operations.jsonl并保持客户可读。可加表/列或兼容视图。

手算：RET_OVER对C1_RIGHT重复引用合计5，大于4，整次失败且不占额。RET_OK后C1_LEFT余额0、C1_RIGHT余额1，SALE2的X额度仍2。v2把RIGHT每套改成1，不能改旧SALE1的4；SALE3/v2新RIGHT额度为1。重放RET_OK不再消耗。
