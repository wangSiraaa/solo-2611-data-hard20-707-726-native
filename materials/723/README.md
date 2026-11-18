材料边界：只提供真实历史格式、足够的原文、修订请求及旧只读客户端，不提供旧后端，也未实现目标迁移/日期/分组服务。
运行起点：python3 legacy_reader.py legacy_report.json --people legacy_people.json。
raw_date权威，cached_day仅旧派生缓存。legacy_report.json是需保留身份与原文的历史已交付快照，历史口径错误已公开，不得据它判断新口径正确。可在副本升级客户端及导入数据，保留原历史文件。

新产品合同partial-date-groups/2：
format、report_id、date_contract（取partial-date-intervals）、source_revisions（person_id到修订号）、windows、known（count/person_ids）、unknown（count/person_ids）、groups、outside。
groups每项group_id/certain_count/possible_count/certain_ids/possible_ids/contributions；outside同样给确定和可能计数/人员。contributions逐人保留raw_date、precision、lower、upper及关系依据，UNKNOWN不造lower/upper。可加字段。升级客户端对旧产品按历史字段原样显示，对新产品显示确定/可能/未知及身份，不把可能人数冒充确定人数。它只读产品，不访问后台数据库。

手算初始：已知4，未知P3一人。W1确定P2一人，可能P1/P2/P4三人；W2确定0，可能P1/P4两人。并集之外确定P5一人，可能P4/P5两人；P1虽可能属于两个组，却必在两组并集中，不是可能在外。
DATE_FIX_1后P1为3月20日：W1确定仍P2，可能P2/P4；W2确定P1，可能P1/P4；在外不变。历史HIST_DATE_001仍保持原值。
DATE_CONFLICT含同号异文及非法2月30日，整批拒绝。窗口之间有空档时，在外判断须针对并集，不是从各组可能计数相减。
