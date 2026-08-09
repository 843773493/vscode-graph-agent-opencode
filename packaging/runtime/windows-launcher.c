#define WIN32_LEAN_AND_MEAN
#define _UNICODE

#include <conio.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#include <windows.h>

static BOOL append_text(
    wchar_t *buffer,
    size_t capacity,
    size_t *length,
    const wchar_t *text
) {
    size_t text_length = wcslen(text);
    if (*length + text_length + 1 > capacity) {
        return FALSE;
    }
    wmemcpy(buffer + *length, text, text_length);
    *length += text_length;
    buffer[*length] = L'\0';
    return TRUE;
}

static BOOL append_repeated(
    wchar_t *buffer,
    size_t capacity,
    size_t *length,
    wchar_t character,
    size_t count
) {
    if (*length + count + 1 > capacity) {
        return FALSE;
    }
    for (size_t index = 0; index < count; index += 1) {
        buffer[*length] = character;
        *length += 1;
    }
    buffer[*length] = L'\0';
    return TRUE;
}

/* 按 Windows 命令行解析规则转义用户参数，避免参数中的空格或引号被截断。 */
static BOOL append_quoted_argument(
    wchar_t *buffer,
    size_t capacity,
    size_t *length,
    const wchar_t *argument
) {
    size_t backslashes = 0;
    if (!append_text(buffer, capacity, length, L"\"")) {
        return FALSE;
    }
    for (const wchar_t *cursor = argument;; cursor += 1) {
        if (*cursor == L'\\') {
            backslashes += 1;
            continue;
        }
        if (*cursor == L'\"') {
            if (!append_repeated(buffer, capacity, length, L'\\', backslashes * 2 + 1) ||
                !append_text(buffer, capacity, length, L"\"")) {
                return FALSE;
            }
            backslashes = 0;
            continue;
        }
        if (*cursor == L'\0') {
            if (!append_repeated(buffer, capacity, length, L'\\', backslashes * 2)) {
                return FALSE;
            }
            break;
        }
        if (!append_repeated(buffer, capacity, length, L'\\', backslashes) ||
            !append_repeated(buffer, capacity, length, *cursor, 1)) {
            return FALSE;
        }
        backslashes = 0;
    }
    return append_text(buffer, capacity, length, L"\"");
}

static BOOL join_path(
    wchar_t *output,
    size_t capacity,
    const wchar_t *directory,
    const wchar_t *relative_path
) {
    int written = _snwprintf_s(
        output,
        capacity,
        _TRUNCATE,
        L"%ls\\%ls",
        directory,
        relative_path
    );
    return written >= 0;
}

static void pause_after_error(void) {
    if (GetConsoleWindow() != NULL && _wgetenv(L"BOXTEAM_NO_PAUSE") == NULL) {
        fputws(L"Press Enter to exit...", stderr);
        fflush(stderr);
        _getwch();
        fputws(L"\n", stderr);
    }
}

static int fail_with_message(const wchar_t *message) {
    fwprintf(stderr, L"BoxTeam launcher failed: %ls\n", message);
    pause_after_error();
    return 1;
}

int wmain(int argument_count, wchar_t **arguments) {
    wchar_t module_path[32768];
    DWORD module_length = GetModuleFileNameW(NULL, module_path, _countof(module_path));
    if (module_length == 0 || module_length >= _countof(module_path)) {
        return fail_with_message(L"cannot resolve the launcher path");
    }
    wchar_t *last_separator = wcsrchr(module_path, L'\\');
    if (last_separator == NULL) {
        return fail_with_message(L"cannot resolve the launcher directory");
    }
    wchar_t executable_name[256];
    wcsncpy_s(executable_name, _countof(executable_name), last_separator + 1, _TRUNCATE);
    *last_separator = L'\0';

    wchar_t manifest_path[32768];
    wchar_t node_path[32768];
    wchar_t launcher_path[32768];
    if (!join_path(manifest_path, _countof(manifest_path), module_path, L"runtime\\runtime-manifest.json") ||
        !join_path(node_path, _countof(node_path), module_path, L"runtime\\node\\node.exe") ||
        !join_path(launcher_path, _countof(launcher_path), module_path, L"launcher\\bin\\boxteam.mjs")) {
        return fail_with_message(L"the installed path is too long");
    }
    if (!SetEnvironmentVariableW(L"BOXTEAM_RUNTIME_MANIFEST", manifest_path)) {
        return fail_with_message(L"cannot set BOXTEAM_RUNTIME_MANIFEST");
    }
    if (!SetCurrentDirectoryW(module_path)) {
        return fail_with_message(L"cannot set the installed directory as the working directory");
    }

    wchar_t command_line[32768] = L"";
    size_t command_length = 0;
    if (!append_quoted_argument(command_line, _countof(command_line), &command_length, node_path) ||
        !append_text(command_line, _countof(command_line), &command_length, L" ") ||
        !append_quoted_argument(command_line, _countof(command_line), &command_length, launcher_path)) {
        return fail_with_message(L"the command line is too long");
    }
    if (wcsstr(executable_name, L"Doctor") != NULL &&
        !append_text(command_line, _countof(command_line), &command_length, L" doctor")) {
        return fail_with_message(L"the command line is too long");
    }
    for (int index = 1; index < argument_count; index += 1) {
        if (!append_text(command_line, _countof(command_line), &command_length, L" ") ||
            !append_quoted_argument(command_line, _countof(command_line), &command_length, arguments[index])) {
            return fail_with_message(L"the command line is too long");
        }
    }

    STARTUPINFOW startup_info;
    PROCESS_INFORMATION process_info;
    ZeroMemory(&startup_info, sizeof(startup_info));
    ZeroMemory(&process_info, sizeof(process_info));
    startup_info.cb = sizeof(startup_info);
    startup_info.dwFlags = STARTF_USESTDHANDLES;
    startup_info.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    startup_info.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    startup_info.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    if (!CreateProcessW(
        node_path,
        command_line,
        NULL,
        NULL,
        TRUE,
        CREATE_UNICODE_ENVIRONMENT,
        NULL,
        module_path,
        &startup_info,
        &process_info
    )) {
        wchar_t error_message[256];
        _snwprintf_s(
            error_message,
            _countof(error_message),
            _TRUNCATE,
            L"cannot start the bundled Node runtime (Windows error %lu)",
            GetLastError()
        );
        return fail_with_message(error_message);
    }

    CloseHandle(process_info.hThread);
    WaitForSingleObject(process_info.hProcess, INFINITE);
    DWORD exit_code = 1;
    GetExitCodeProcess(process_info.hProcess, &exit_code);
    CloseHandle(process_info.hProcess);
    if (exit_code != 0) {
        wchar_t error_message[256];
        _snwprintf_s(
            error_message,
            _countof(error_message),
            _TRUNCATE,
            L"BoxTeam exited with code %lu",
            exit_code
        );
        return fail_with_message(error_message);
    }
    return 0;
}
