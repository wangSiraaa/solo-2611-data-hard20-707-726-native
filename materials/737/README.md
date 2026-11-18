# 737 合成输入与引号保真接入

本目录没有解码器或客户库；input.json给初始客户、两个明确字段映射和实际操作。原始CSV采用CRLF，first.csv中的u2.memo以多字节汉字甲开头并含一个真实CRLF，不是两个字符\r\n。文件必须按原件读取，不能先去引号重写。

f1后u1为name=TEXT(Ada)、memo=TEXT(空字符串)、category=NULL。u2.name为TEXT(NULL)，memo为带换行文字。f2按p2映射同名note第一次为category、第二次为memo，u1应为Ada/changed/字面NULL，旧f1回执仍为原结果。新客户没有提供的字段是独立MISSING状态，不是NULL。

记录单元格字节范围含原始引号；id/header本身不执行补丁字段覆盖。示例只提供输入与手算锚点，未预做目标实现。
