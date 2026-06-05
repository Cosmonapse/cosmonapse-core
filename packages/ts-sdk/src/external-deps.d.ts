/**
 * (Intentionally empty.)
 *
 * The optional, lazily-imported drivers `better-sqlite3` and `pg` ship no type
 * declarations. Rather than shim them here (an ambient `declare module` is
 * ignored once the package is physically installed, so it doesn't prevent the
 * .d.ts build from erroring), the adapters in `storage-sqlite.ts` /
 * `storage-postgres.ts` indirect the dynamic-import specifier through a string
 * variable. That stops tsc from statically resolving the driver's types while
 * keeping the import lazy and external at runtime  -  so no shim is needed.
 */

export {};
