import type { LlmNode, NodePool } from '@/api/types'

/** 打开节点编辑器的方式：编辑已有节点，或在某个池里新建（可从 template 复制地址和 Key）。 */
export type NodeEditorTarget = { node: LlmNode } | { pool: NodePool; template?: LlmNode }
