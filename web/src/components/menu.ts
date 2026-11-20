import type { IconName } from './icons'

/** “更多”菜单的一项：分隔线、小标题或可执行的操作。 */
export type MenuItem =
  | '-'
  | { heading: string }
  | { label: string; icon: IconName; danger?: boolean; run: () => unknown }
