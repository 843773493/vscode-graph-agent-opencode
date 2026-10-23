/* 由当前连接提供 SQLite API，不链接或加载另一份 SQLite。 */
#include <sqlite3ext.h>
SQLITE_EXTENSION_INIT1

static void max_pathname(sqlite3_context *context, int argc, sqlite3_value **argv) {
    sqlite3_vfs *vfs = sqlite3_vfs_find(0);
    (void)argc;
    (void)argv;
    if (!vfs || vfs->iVersion < 1 || vfs->szOsFile < 0 || vfs->mxPathname < 1) {
        sqlite3_result_error(context, "SQLite default VFS header is invalid", -1);
        return;
    }
    sqlite3_result_int(context, vfs->mxPathname);
}

int sqlite3_extension_init(
    sqlite3 *database, char **error, const sqlite3_api_routines *api
) {
    (void)error;
    SQLITE_EXTENSION_INIT2(api);
    return sqlite3_create_function(
        database, "boxteam_vfs_max_pathname", 0,
        SQLITE_UTF8 | SQLITE_DIRECTONLY, 0, max_pathname, 0, 0
    );
}
