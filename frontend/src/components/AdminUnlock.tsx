/** Superseded by the named-session login (LoginScreen + UserMenu).
 *
 *  Admin is now decided at sign-in — supplying the admin password instead of
 *  the team one grants it — so a separate unlock panel would be a second way to
 *  do the same thing, and a second thing to keep working.
 *
 *  Kept as an empty module rather than deleted so an older build that still
 *  imports it fails loudly at review time instead of silently at runtime.
 */
export {};
