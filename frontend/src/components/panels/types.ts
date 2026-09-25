export interface PanelProps {
  sessionId: string;
  /** 后端阶段已推进后刷新会话状态（成功写产物后调用） */
  onAdvance: () => Promise<void> | void;
  /** 仅本地切换查看节点（回退/进入下一屏，不改变后端阶段） */
  onJump: (index: number) => void;
  onError: (message: string | null) => void;
  /** 回看历史节点时为 true，隐藏写操作 */
  readOnly: boolean;
}
