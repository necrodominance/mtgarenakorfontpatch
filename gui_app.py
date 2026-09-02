from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from gui_config import default_gui_config
from managed_patch import apply_managed_patch, inspect_managed_status, restore_managed_backup


APP_TITLE = 'MTGA 한국어 폰트 패처'
DEFAULT_GAMEPATHS = (
    Path(r'C:\Program Files (x86)\Steam\steamapps\common\MTGA'),
    Path(r'C:\Program Files\Steam\steamapps\common\MTGA'),
)


def application_dir() -> Path:
    """Directory that contains the executable (or this source file)."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent



def default_game_path() -> str:
    for candidate in DEFAULT_GAMEPATHS:
        if (candidate / 'MTGA_Data').is_dir():
            return str(candidate)
    return str(DEFAULT_GAMEPATHS[0])


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry('820x700')
        self.minsize(720, 600)

        self._busy = False
        self._worker_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        cfg = default_gui_config(application_dir())
        self.game_var = tk.StringVar(value=default_game_path())
        self.default_font_var = tk.StringVar(value=str(cfg.default_font))
        self.title_font_var = tk.StringVar(value=str(cfg.title_font))
        self.default_size_var = tk.IntVar(value=cfg.default_size_percent)
        self.title_size_var = tk.IntVar(value=cfg.title_size_percent)
        self.default_bold_var = tk.DoubleVar(value=cfg.default_bold_style)
        self.title_bold_var = tk.DoubleVar(value=cfg.title_bold_style)
        self.beleren_var = tk.BooleanVar(value=cfg.beleren_ascii)
        self.status_var = tk.StringVar(value='확인하지 않음')

        self._configure_style()
        self._build_ui()

        self._log('기본 폰트: Pretendard-Medium.ttf / Hahmlet-SemiBold.ttf')
        self._log('기본 크기: Default 100% / Title 100%, synthetic Bold: 0.75 / 0.75')
        self.after(250, self.refresh_status)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use('vista')
        except tk.TclError:
            pass
        style.configure('Title.TLabel', font=('', 17, 'bold'))
        style.configure('Action.TButton', padding=(10, 8))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=18)
        root.pack(fill='both', expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(6, weight=1)

        ttk.Label(root, text='MTGA 한국어 폰트 패처', style='Title.TLabel').grid(
            row=0, column=0, sticky='w'
        )
        ttk.Label(
            root,
            text='한국어 Default/Title 폰트만 교체합니다. 영어 Default와 정적 Beleren은 원본을 유지합니다.',
            wraplength=760,
            justify='left',
        ).grid(row=1, column=0, sticky='ew', pady=(4, 12))

        paths = ttk.LabelFrame(root, text='경로', padding=10)
        paths.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        paths.columnconfigure(1, weight=1)
        self._add_path_row(paths, 0, 'MTGA 설치 경로', self.game_var, self._browse_game)
        self._add_path_row(
            paths,
            1,
            'Default 폰트',
            self.default_font_var,
            lambda: self._browse_font(self.default_font_var),
        )
        self._add_path_row(
            paths,
            2,
            'Title 폰트',
            self.title_font_var,
            lambda: self._browse_font(self.title_font_var),
        )

        options = ttk.LabelFrame(root, text='폰트 옵션', padding=10)
        options.grid(row=3, column=0, sticky='ew', pady=(0, 10))
        options.columnconfigure(1, weight=1)

        self.default_size_spin = ttk.Spinbox(
            options, from_=50, to=200, textvariable=self.default_size_var, width=10
        )
        self.title_size_spin = ttk.Spinbox(
            options, from_=50, to=200, textvariable=self.title_size_var, width=10
        )
        self.default_bold_spin = ttk.Spinbox(
            options,
            from_=0.0,
            to=1.0,
            increment=0.05,
            textvariable=self.default_bold_var,
            width=10,
            format='%.2f',
        )
        self.title_bold_spin = ttk.Spinbox(
            options,
            from_=0.0,
            to=1.0,
            increment=0.05,
            textvariable=self.title_bold_var,
            width=10,
            format='%.2f',
        )

        self._add_option_row(options, 0, 'Default 크기', self.default_size_spin, '%')
        self._add_option_row(options, 1, 'Title 크기', self.title_size_spin, '%')
        self._add_option_row(options, 2, 'Default synthetic Bold', self.default_bold_spin)
        self._add_option_row(options, 3, 'Title synthetic Bold', self.title_bold_spin)
        ttk.Checkbutton(
            options,
            text='Title의 영문·숫자·기호에 원본 Beleren 사용',
            variable=self.beleren_var,
        ).grid(row=4, column=0, columnspan=3, sticky='w', pady=(5, 0))

        info_and_status = ttk.Frame(root)
        info_and_status.grid(row=4, column=0, sticky='ew', pady=(0, 10))
        info_and_status.columnconfigure(0, weight=1)
        ttk.Label(
            info_and_status,
            text=(
                '백업 정책: 폰트 영역이 순정일 때 최초 1회만 백업합니다. 패치 후 재설정은 같은 순정 백업을 '
                '사용하며, 게임의 폰트/활성 번들 세대가 바뀌면 구버전 백업을 자동 폐기합니다.'
            ),
            wraplength=760,
            justify='left',
        ).grid(row=0, column=0, sticky='ew', pady=(0, 8))

        status_box = ttk.LabelFrame(info_and_status, text='게임 상태', padding=10)
        status_box.grid(row=1, column=0, sticky='ew')
        status_box.columnconfigure(0, weight=1)
        ttk.Label(status_box, textvariable=self.status_var).grid(row=0, column=0, sticky='w')
        self.refresh_button = ttk.Button(status_box, text='상태 새로고침', command=self.refresh_status)
        self.refresh_button.grid(row=0, column=1, sticky='e', padx=(8, 0))

        buttons = ttk.Frame(root)
        buttons.grid(row=5, column=0, sticky='ew', pady=(0, 10))
        buttons.columnconfigure(0, weight=2)
        buttons.columnconfigure(1, weight=1)
        self.patch_button = ttk.Button(
            buttons, text='패치 적용', command=self.apply_patch, style='Action.TButton'
        )
        self.patch_button.grid(row=0, column=0, sticky='ew', padx=(0, 5))
        self.restore_button = ttk.Button(
            buttons,
            text='순정 백업으로 복구',
            command=self.restore_backup,
            style='Action.TButton',
        )
        self.restore_button.grid(row=0, column=1, sticky='ew', padx=(5, 0))

        self.log = scrolledtext.ScrolledText(root, wrap='word', height=12, state='disabled')
        self.log.grid(row=6, column=0, sticky='nsew')

    @staticmethod
    def _add_path_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        callback: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=3)
        ttk.Button(parent, text='찾아보기…', command=callback).grid(
            row=row, column=2, padx=(8, 0), pady=3
        )

    @staticmethod
    def _add_option_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        widget: ttk.Spinbox,
        suffix: str = '',
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=3)
        widget.grid(row=row, column=1, sticky='w', pady=3)
        if suffix:
            ttk.Label(parent, text=suffix).grid(row=row, column=2, sticky='w', padx=(4, 0), pady=3)

    def _browse_game(self) -> None:
        initial = self.game_var.get().strip().strip('"')
        path = filedialog.askdirectory(title='MTGA 설치 경로 선택', initialdir=initial or None)
        if path:
            self.game_var.set(path)

    def _browse_font(self, variable: tk.StringVar) -> None:
        current = Path(variable.get().strip().strip('"'))
        initial_dir = str(current.parent) if current.parent.exists() else str(application_dir())
        path = filedialog.askopenfilename(
            title='폰트 선택',
            initialdir=initial_dir,
            filetypes=(('Font files', '*.ttf *.otf'), ('All files', '*.*')),
        )
        if path:
            variable.set(path)

    def _log(self, text: str) -> None:
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = 'disabled' if busy else 'normal'
        self.patch_button.configure(state=state)
        self.restore_button.configure(state=state)
        self.refresh_button.configure(state=state)

    def _start(self, label: str, fn: Callable[[], Any], on_success: Callable[[Any], None]) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._log(label)

        def worker() -> None:
            try:
                self._worker_queue.put(('success', (on_success, fn())))
            except Exception as exc:
                self._worker_queue.put(('failure', f'{type(exc).__name__}: {exc}'))
            finally:
                self._worker_queue.put(('finished', None))

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, self._poll_worker)

    def _poll_worker(self) -> None:
        finished = False
        while True:
            try:
                event, payload = self._worker_queue.get_nowait()
            except queue.Empty:
                break

            if event == 'success':
                callback, result = payload
                callback(result)
            elif event == 'failure':
                self._on_failure(payload)
            elif event == 'finished':
                finished = True

        if finished:
            self._set_busy(False)
        elif self._busy:
            self.after(50, self._poll_worker)

    def _on_failure(self, message: str) -> None:
        self._log('오류: ' + message)
        messagebox.showerror(APP_TITLE, message, parent=self)

    def _gamepath(self) -> Path:
        return Path(self.game_var.get().strip().strip('"'))

    def _validate_inputs(self) -> tuple[Path, Path, Path]:
        game = self._gamepath()
        default_font = Path(self.default_font_var.get().strip().strip('"'))
        title_font = Path(self.title_font_var.get().strip().strip('"'))
        if not (game / 'MTGA_Data').is_dir():
            raise ValueError('MTGA_Data가 있는 올바른 게임 설치 경로를 선택하세요.')
        if not default_font.is_file():
            raise ValueError(f'Default 폰트를 찾을 수 없습니다: {default_font}')
        if not title_font.is_file():
            raise ValueError(f'Title 폰트를 찾을 수 없습니다: {title_font}')
        return game, default_font, title_font

    def refresh_status(self) -> None:
        game = self._gamepath()
        if not (game / 'MTGA_Data').is_dir():
            self.status_var.set('잘못된 MTGA 경로')
            return

        def done(result: dict) -> None:
            labels = {
                'pristine': '순정',
                'patched': '패치됨',
                'unknown': '알 수 없는 수정',
                'incompatible': '호환되지 않음',
            }
            text = labels.get(result['state'], result['state'])
            if result.get('backup_usable'):
                backup_label = '사용 가능'
            elif result.get('backup_exists'):
                backup_label = '사용 불가'
            else:
                backup_label = '없음'
            text += ' / 백업: ' + backup_label
            if result.get('backup_deleted_as_stale'):
                text += ' (구버전 백업 자동 삭제됨)'
                self._log('게임 세대 변경을 감지해 구버전 백업을 삭제했습니다.')
            self.status_var.set(text)
            if result.get('message'):
                self._log(result['message'])
            self._log(f'상태: {text}')

        self._start('게임 폰트 상태를 확인합니다…', lambda: inspect_managed_status(game), done)

    def _start_patch_after_preflight(
        self,
        game: Path,
        default_font: Path,
        title_font: Path,
        args: dict[str, Any],
        status: dict,
    ) -> None:
        state = status.get('state')
        backup_exists = bool(status.get('backup_exists'))
        backup_usable = bool(status.get('backup_usable'))

        if state == 'patched' and backup_usable:
            answer = messagebox.askyesno(
                APP_TITLE,
                '현재 게임에 폰트 패치가 적용되어 있고, 이 릴리즈에서 사용할 수 있는 순정 백업이 있습니다.\n\n'
                '패치를 계속하면 먼저 순정 백업으로 복구한 뒤 새 설정으로 다시 패치합니다.\n'
                '계속하시겠습니까?',
                parent=self,
            )
            if not answer:
                self._log('재패치를 취소했습니다.')
                return
        elif state == 'patched' and not backup_usable:
            if backup_exists:
                backup_problem = (
                    '현재 게임에 폰트 패치가 적용되어 있지만 남아 있는 백업은 이전 형식이거나 '
                    '검증에 실패하여 사용할 수 없습니다.\n\n'
                )
            else:
                backup_problem = (
                    '현재 게임에 폰트 패치가 적용되어 있지만 이 릴리즈에서 사용할 수 있는 '
                    '순정 백업이 없습니다.\n\n'
                )
            messagebox.showwarning(
                APP_TITLE,
                backup_problem
                + '구형 릴리즈의 백업은 사용하지 않습니다. Steam에서 MTGA의 "설치된 파일 무결성 검사"를 실행해 '
                + '순정 상태로 복구한 뒤 다시 패치해 주세요.',
                parent=self,
            )
            return
        elif state == 'unknown':
            messagebox.showwarning(
                APP_TITLE,
                '현재 MTGA 폰트 영역에 이 릴리즈가 안전하게 복구할 수 없는 수정이 감지되었습니다.\n\n'
                '이전 릴리즈로 패치한 경우도 여기에 포함될 수 있습니다. 구형 백업은 사용하지 않습니다.\n'
                'Steam에서 MTGA의 "설치된 파일 무결성 검사"를 실행해 순정 상태로 복구한 뒤 다시 패치해 주세요.',
                parent=self,
            )
            return
        elif state == 'incompatible':
            messagebox.showwarning(
                APP_TITLE,
                '현재 MTGA 버전의 폰트 자산은 이 패처와 호환되지 않습니다.',
                parent=self,
            )
            return

        def done(result: dict) -> None:
            if result.get('backup_deleted_as_stale'):
                self._log('구버전 현행 백업을 폐기했습니다.')
            if result.get('backup_created'):
                self._log('현재 순정 폰트 영역을 최초 백업했습니다.')
            if result.get('restored_before_patch'):
                self._log('현행 순정 백업으로 먼저 복구한 뒤 새 설정으로 다시 패치했습니다.')
            self._log('패치 적용 및 구조 검증이 완료되었습니다.')
            self.status_var.set('패치됨 / 백업: 있음')
            messagebox.showinfo(APP_TITLE, '폰트 패치를 적용했습니다.', parent=self)

        self.after(
            0,
            lambda: self._start(
                '폰트 패치를 적용합니다… 게임을 실행 중이라면 종료해 두는 것을 권장합니다.',
                lambda: apply_managed_patch(game, default_font, title_font, **args),
                done,
            ),
        )

    def apply_patch(self) -> None:
        try:
            game, default_font, title_font = self._validate_inputs()
            default_size = int(self.default_size_var.get())
            title_size = int(self.title_size_var.get())
            default_bold = float(self.default_bold_var.get())
            title_bold = float(self.title_bold_var.get())
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, str(exc), parent=self)
            return

        if not 50 <= default_size <= 200 or not 50 <= title_size <= 200:
            messagebox.showwarning(APP_TITLE, '폰트 크기는 50~200% 범위로 입력하세요.', parent=self)
            return
        if not 0.0 <= default_bold <= 1.0 or not 0.0 <= title_bold <= 1.0:
            messagebox.showwarning(APP_TITLE, 'synthetic Bold 값은 0.00~1.00 범위로 입력하세요.', parent=self)
            return

        beleren_ascii = bool(self.beleren_var.get())
        args = dict(
            beleren_ascii=beleren_ascii,
            default_scale=default_size / 100.0,
            title_scale=title_size / 100.0,
            default_bold_style=default_bold,
            title_bold_style=title_bold,
        )
        self._log(
            f"설정: Default {default_size}% / Bold {default_bold:.2f}, "
            f"Title {title_size}% / Bold {title_bold:.2f}, "
            f"Beleren {'ON' if beleren_ascii else 'OFF'}"
        )

        def preflight_done(status: dict) -> None:
            self._start_patch_after_preflight(game, default_font, title_font, args, status)

        self._start(
            '패치 전 현재 게임/백업 상태를 확인합니다…',
            lambda: inspect_managed_status(game),
            preflight_done,
        )

    def restore_backup(self) -> None:
        game = self._gamepath()
        if not (game / 'MTGA_Data').is_dir():
            messagebox.showwarning(APP_TITLE, '올바른 MTGA 설치 경로를 선택하세요.', parent=self)
            return
        answer = messagebox.askyesno(
            APP_TITLE,
            '현재 게임 세대의 순정 백업으로 폰트 관련 파일을 복구할까요?',
            parent=self,
        )
        if not answer:
            return

        def done(result: dict) -> None:
            self.status_var.set('순정 / 백업: 있음')
            self._log('순정 백업으로 복구하고 폰트 상태를 검증했습니다.')
            messagebox.showinfo(APP_TITLE, '순정 백업으로 복구했습니다.', parent=self)

        self._start('순정 백업을 검증하고 복구합니다…', lambda: restore_managed_backup(game), done)


def main() -> int:
    window = MainWindow()
    window.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
