# 720 配方来源与标签操作

catalog.json只有新制作输入：完整映射v1、配方mix/v1、线接触v1及批次配置v1。operations.json明确先交付首标签，再改实际选择，最后只改rice映射。publish_mapping表示以当前完整目录为基础替换所指成分后发布新完整版本，未指成分继承；不是仅把补丁当完整目录。没有旧配方/标签实现。

首标签实际oat/base用量2和milk/drink用量1：contains={gluten,milk}，may_contain={nut}。gluten来源oat/base；milk来源milk/drink，未采用的wheat和rice不是来源。

配置v2改为oat+rice：contains={gluten}，may_contain={milk,nut}。stale-selection不得交付。映射v2把rice的空集合改为{nut}，即使实际选择不变，stale-mapping也不得交付；新标签contains={gluten,nut}，may_contain={milk}，nut来源rice/drink。完整映射版本变化一律使待交付标签失效，是公开合同；不要求复杂局部依赖失效算法。

首个已交付标签仍绑定配置v1/映射v1/线v1，不能被上述变化覆盖。未知映射与已定义空集合不同。读取标签时应能看到实际槽、成分、映射版本与过敏原来源，而不只是两列集合。
