from __future__ import annotations

from pathlib import Path
import sys

from .font_sources import find_adjacent_default_fonts


def application_directory() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def initial_font_values(base_dir: str | Path) -> dict[str, str]:
    found = find_adjacent_default_fonts(base_dir)
    return {
        'default': str(found.default_path or ''),
        'title': str(found.title_path or ''),
        'bold': str(found.title_bold_path or ''),
    }


def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from .final import VERSION, apply_final_patch, inspect_final_status
    from .font_sources import FontSelection, inspect_font
    from .gui_tasks import BackgroundJob
    from .runtime import discover_installation, restore_latest

    root = tk.Tk()
    root.title(f'mtgarenakrfontpatch')
    root.minsize(820, 620)
    root.geometry('860x680')

    style = ttk.Style(root)
    style.configure('Header.TLabel', font=('', 17, 'bold'))
    style.configure('Subtle.TLabel', foreground='#666666')
    style.configure('Primary.TButton', font=('', 10, 'bold'), padding=(14, 8))

    defaults_dir = application_directory()
    initial = initial_font_values(defaults_dir)

    vars = {
        'root': tk.StringVar(),
        'default': tk.StringVar(value=initial['default']),
        'title': tk.StringVar(value=initial['title']),
        'bold': tk.StringVar(value=initial['bold']),
        'default_scale': tk.StringVar(value='100'),
        'title_scale': tk.StringVar(value='100'),
        'advanced': tk.BooleanVar(value=False),
        'status': tk.StringVar(value='준비됨'),
    }

    outer = ttk.Frame(root, padding=18)
    outer.pack(fill='both', expand=True)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(5, weight=1)

    install_box = ttk.LabelFrame(outer, text='게임 위치', padding=12)
    install_box.grid(row=2, column=0, sticky='ew', pady=(0, 10))
    install_box.columnconfigure(1, weight=1)

    ttk.Label(install_box, text='설치 경로').grid(row=0, column=0, sticky='w', padx=(0, 10))
    ttk.Entry(install_box, textvariable=vars['root']).grid(row=0, column=1, sticky='ew')

    def browse_dir() -> None:
        value = filedialog.askdirectory(title='설치 폴더 선택')
        if value:
            vars['root'].set(value)

    ttk.Button(install_box, text='찾기…', command=browse_dir).grid(row=0, column=2, padx=(8, 0))
    ttk.Label(
        install_box,
        text='비워두면 기본 스팀 폴더가 선택됩니다.',
        style='Subtle.TLabel',
    ).grid(row=1, column=1, sticky='w', pady=(5, 0))

    font_box = ttk.LabelFrame(outer, text='사용할 폰트', padding=12)
    font_box.grid(row=3, column=0, sticky='ew', pady=(0, 10))
    font_box.columnconfigure(1, weight=1)

    def browse_font(key: str) -> None:
        value = filedialog.askopenfilename(
            title='폰트 선택',
            filetypes=[('OpenType / TrueType', '*.ttf *.otf'), ('모든 파일', '*.*')],
        )
        if value:
            vars[key].set(value)

    font_rows = [
        ('Default', 'default', 'Pretendard-Medium.ttf'),
        ('Title', 'title', 'Hahmlet-SemiBold.ttf'),
        ('Title Bold', 'bold', 'Hahmlet-Bold.ttf'),
    ]
    for row, (label, key, hint) in enumerate(font_rows):
        ttk.Label(font_box, text=label, width=23).grid(row=row, column=0, sticky='w', pady=4)
        ttk.Entry(font_box, textvariable=vars[key]).grid(row=row, column=1, sticky='ew', pady=4)
        ttk.Button(font_box, text='변경…', command=lambda k=key: browse_font(k)).grid(
            row=row, column=2, padx=(8, 0), pady=4
        )
        ttk.Label(font_box, text=hint, style='Subtle.TLabel').grid(
            row=row, column=3, sticky='w', padx=(8, 0), pady=4
        )

    def append_log(message: str) -> None:
        log.configure(state='normal')
        log.insert('end', message.rstrip() + '\n')
        log.see('end')
        log.configure(state='disabled')

    def load_defaults() -> None:
        values = initial_font_values(defaults_dir)
        for key in ('default', 'title', 'bold'):
            vars[key].set(values[key])
        missing = [
            name for key, name in (
                ('default', 'Pretendard-Medium'),
                ('title', 'Hahmlet-SemiBold'),
                ('bold', 'Hahmlet-Bold'),
            ) if not values[key]
        ]
        if missing:
            vars['status'].set('일부 기본 폰트를 찾지 못했습니다.')
            append_log('기본 폰트 누락: ' + ', '.join(missing))
        else:
            vars['status'].set('기본 폰트를 자동으로 불러왔습니다.')
            append_log(f'기본 폰트 불러옴: {defaults_dir}')

    font_actions = ttk.Frame(font_box)
    font_actions.grid(row=len(font_rows), column=0, columnspan=4, sticky='ew', pady=(8, 0))
    ttk.Button(font_actions, text='기본 폰트 다시 불러오기', command=load_defaults).pack(side='left')
    ttk.Label(
        font_actions,
        text='Title Bold가 비어 있으면 Title에 선택된 폰트를 사용합니다.',
        style='Subtle.TLabel',
    ).pack(side='left', padx=(12, 0))

    advanced_box = ttk.LabelFrame(outer, text='고급 설정 (실험용)', padding=10)
    advanced_box.grid(row=4, column=0, sticky='ew', pady=(0, 10))
    advanced_box.columnconfigure(1, weight=1)

    advanced_body = ttk.Frame(advanced_box)

    def toggle_advanced() -> None:
        if vars['advanced'].get():
            advanced_body.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        else:
            advanced_body.grid_remove()

    ttk.Checkbutton(
        advanced_box,
        text='글자 크기 배율 조정',
        variable=vars['advanced'],
        command=toggle_advanced,
    ).grid(row=0, column=0, sticky='w')

    ttk.Label(advanced_body, text='Default 크기 (%)').grid(row=0, column=0, sticky='w', padx=(0, 8), pady=3)
    ttk.Spinbox(advanced_body, from_=50, to=200, increment=1, width=8, textvariable=vars['default_scale']).grid(
        row=0, column=1, sticky='w', pady=3
    )
    ttk.Label(advanced_body, text='Title 크기 (%)').grid(row=1, column=0, sticky='w', padx=(0, 8), pady=3)
    ttk.Spinbox(advanced_body, from_=50, to=200, increment=1, width=8, textvariable=vars['title_scale']).grid(
        row=1, column=1, sticky='w', pady=3
    )
    advanced_body.grid_remove()

    activity_box = ttk.LabelFrame(outer, text='진행 상태', padding=10)
    activity_box.grid(row=5, column=0, sticky='nsew', pady=(0, 10))
    activity_box.columnconfigure(0, weight=1)
    activity_box.rowconfigure(2, weight=1)

    ttk.Label(activity_box, textvariable=vars['status']).grid(row=0, column=0, sticky='w')
    progressbar = ttk.Progressbar(activity_box, mode='indeterminate')
    progressbar.grid(row=1, column=0, sticky='ew', pady=(6, 8))

    log_frame = ttk.Frame(activity_box)
    log_frame.grid(row=2, column=0, sticky='nsew')
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log = tk.Text(log_frame, height=9, wrap='word', state='disabled')
    log.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=log.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    log.configure(yscrollcommand=scrollbar.set)

    def selection() -> FontSelection:
        if not vars['default'].get().strip() or not vars['title'].get().strip():
            raise ValueError('Default 및 Title 폰트를 선택하세요.')
        return FontSelection(
            default_path=Path(vars['default'].get()),
            title_path=Path(vars['title'].get()),
            title_bold_path=Path(vars['bold'].get()) if vars['bold'].get().strip() else None,
            default_scale=float(vars['default_scale'].get()),
            title_scale=float(vars['title_scale'].get()),
        )

    def install_argument() -> Path | None:
        value = vars['root'].get().strip()
        return Path(value) if value else None

    def format_font_info(sel: FontSelection) -> str:
        items = []
        warnings = []
        for label, path in (
            ('Default', sel.default_path),
            ('Title', sel.title_path),
            ('Title Bold', sel.title_bold_path),
        ):
            if path is None:
                continue
            info = inspect_font(path)
            items.append(f'{label}: {info.family} {info.style}')
            if label != 'Title Bold' and not info.has_korean:
                warnings.append(f'{label} 폰트에 한글 글리프가 없습니다.')
        result = ' / '.join(items)
        if warnings:
            result += '\n경고: ' + ' '.join(warnings)
        return result

    job = BackgroundJob()
    action_buttons: list[ttk.Button] = []
    active_success = {'callback': None}

    def set_busy(busy: bool) -> None:
        state = 'disabled' if busy else 'normal'
        for button in action_buttons:
            button.configure(state=state)
        if busy:
            progressbar.start(10)
        else:
            progressbar.stop()

    def poll_job() -> None:
        terminal = False
        for event in job.poll():
            if event.kind == 'progress':
                message = str(event.payload)
                vars['status'].set(message.splitlines()[0])
                append_log(message)
            elif event.kind == 'error':
                terminal = True
                set_busy(False)
                exc = event.payload
                vars['status'].set(f'오류: {exc}')
                append_log(f'오류: {exc}')
                messagebox.showerror('mtgarenakrfontpatch', f'{exc}')
            elif event.kind == 'success':
                terminal = True
                set_busy(False)
                callback = active_success['callback']
                if callback is not None:
                    callback(event.payload)
        if not terminal and job.running:
            root.after(100, poll_job)
        elif not terminal:
            root.after(20, poll_job)

    def launch(work, on_success, initial_status: str) -> None:
        if job.running:
            return
        active_success['callback'] = on_success
        vars['status'].set(initial_status)
        append_log(initial_status)
        set_busy(True)
        try:
            job.start(work)
        except Exception:
            set_busy(False)
            raise
        root.after(50, poll_job)

    def report_error(exc: Exception) -> None:
        vars['status'].set(f'오류: {exc}')
        append_log(f'오류: {exc}')
        messagebox.showerror('mtgarenakrfontpatch', f'{exc}')

    def do_patch() -> None:
        try:
            sel = selection()
            install = install_argument()
        except Exception as exc:
            report_error(exc)
            return

        def work(progress):
            progress('아레나 설치 경로 확인 중…')
            paths = discover_installation(install)
            progress('폰트 검사 중…\n' + format_font_info(sel))
            return apply_final_patch(paths, sel, progress=progress)

        def success(result):
            backup, status = result
            vars['status'].set('패치 완료')
            append_log(
                '패치 완료\n'
                f'백업: {backup}\n'
                f'Default: {status.default_face[0]} {status.default_face[1]}\n'
                f'Title: {status.title_face[0]} {status.title_face[1]}\n'
                f'Title Bold: {status.title_bold_face[0]} {status.title_bold_face[1]}'
            )
            messagebox.showinfo('mtgarenakrfontpatch', '패치가 완료되었습니다.')

        launch(work, success, '패치 준비 중…')

    def do_check() -> None:
        try:
            install = install_argument()
        except Exception as exc:
            report_error(exc)
            return

        def work(progress):
            progress('MTGA 설치 경로 확인 중…')
            paths = discover_installation(install)
            progress('현재 패치 상태 검사 중…')
            return inspect_final_status(paths)

        def success(status):
            text = (
                f'Default: {status.default_face[0]} {status.default_face[1]}\n'
                f'Title: {status.title_face[0]} {status.title_face[1]}\n'
                f'Title Bold: {status.title_bold_face[0]} {status.title_bold_face[1]}\n'
                f'패치 여부: {"패치됨" if status.structurally_patched else "패치되지 않음"}'
            )
            vars['status'].set('상태 확인 완료')
            append_log(text)

        launch(work, success, '상태 확인 준비 중…')

    def do_restore() -> None:
        try:
            install = install_argument()
        except Exception as exc:
            report_error(exc)
            return

        def work(progress):
            progress('MTGA 설치 경로 확인 중…')
            paths = discover_installation(install)
            progress('최근 백업 복원 중…')
            return restore_latest(paths)

        def success(backup):
            vars['status'].set('복원 완료')
            append_log(f'복원 완료: {backup}')
            messagebox.showinfo('mtgarenakrfontpatch', '가장 최근 백업을 복원했습니다.')

        launch(work, success, '복원 준비 중…')

    actions = ttk.Frame(outer)
    actions.grid(row=6, column=0, sticky='ew')
    patch_button = ttk.Button(actions, text='폰트 패치 적용', style='Primary.TButton', command=do_patch)
    patch_button.pack(side='left')
    check_button = ttk.Button(actions, text='현재 상태 확인', command=do_check)
    check_button.pack(side='left', padx=(10, 0))
    restore_button = ttk.Button(actions, text='최근 백업 복원', command=do_restore)
    restore_button.pack(side='left', padx=(10, 0))
    ttk.Button(actions, text='닫기', command=root.destroy).pack(side='right')
    action_buttons.extend((patch_button, check_button, restore_button))

    if initial['default'] and initial['title']:
        append_log(f'기본 폰트 자동 감지: {defaults_dir}')
        vars['status'].set('기본 폰트를 자동으로 선택했습니다.')
    else:
        append_log('기본 폰트를 모두 찾지 못했습니다. 필요한 폰트를 직접 선택하세요.')
        vars['status'].set('폰트를 선택하세요.')

    root.mainloop()
    return 0
