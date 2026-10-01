"""
画像子图的图定义（编译成一个可 .invoke() 的独立 graph 对象）。

 重构（recreate 多pending interrupt框架边界修复，见 实战记录.md 当天条目）：
结构从"START → generate_all_drafts → (Send扇出多字段) → confirm_one_field" 简化为
"START → confirm_one_field 直连"——因为这个子图现在只处理一个字段（由主图循环
分别以独立 thread_id 各自 invoke 一次），不再需要在子图内部先生成草稿、再扇出。
generate_all_drafts（一次性生成全部字段草稿）上移到主图 kam_node.py。

confirm_one_field → regenerate_one_field → confirm_one_field 这条 recreate 循环
路径不变，仍然靠 Command(goto=Send(...)) 动态路由，不用静态边（原因见
sub_profile_node.py 里的说明：recreate 需要"重新 interrupt"，普通 return 做不到）。
"""

from langgraph.graph import START, StateGraph

from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_node import (
    confirm_one_field,
    regenerate_one_field,
)
from src.graphs.kam_graph.kam_sub_graph_profile.sub_profile_state import ProfileSubState

_builder = StateGraph(ProfileSubState)

_builder.add_node("confirm_one_field", confirm_one_field)
_builder.add_node("regenerate_one_field", regenerate_one_field)

# 子图入口直接是 confirm_one_field——传进来的 State 已经是主图 generate_all_drafts
# 生成好的单字段草稿，不需要额外的生成节点。
_builder.add_edge(START, "confirm_one_field")

# confirm_one_field 的 ok/discard 分支直接 return（正常结束，走向 END）；
# recreate 分支用 Command(goto=Send(...)) 动态路由到 regenerate_one_field，
# regenerate_one_field 完成后又用 Command(goto=Send(...)) 动态路由回
# confirm_one_field——两条动态路由都不需要注册静态边。

#  决定（见 实战记录.md 当天条目）：主图子图共用同一个 checkpointer 实例。
# ：现在每个字段各自一个独立 thread_id，仍然共用同一个
# checkpointer/store 实例（只是 config 里的 thread_id 不同），不影响这条决定。


def build_profile_graph(checkpointer, store):
    return _builder.compile(checkpointer=checkpointer, store=store)
