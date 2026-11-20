"""桌面版主程序的入口：PyInstaller 从这里打包 video-turbo-server。"""

import multiprocessing

from video_turbo.orchestrator.app import main

if __name__ == "__main__":
    # 打包后的程序启动子进程时会重新执行入口，必须先交给 multiprocessing 处理。
    multiprocessing.freeze_support()
    main()
