/** Client-side view of the caller's permissions, used only to decide which
 *  controls to render.
 *
 *  This is presentation, never enforcement — every mutating endpoint authorizes
 *  independently on the server. The point is that a user with read-only access
 *  to a vendor shouldn't be offered buttons that can only ever 403.
 *
 *  The rules below mirror `Principal` in `backend/app/core/authz.py`; keep them
 *  in step with it:
 *    - `see_all` (admin owner, or auth disabled) reads every vendor, but writes
 *      are still capped by the *effective* role — so an admin's downgraded
 *      read_only API key gets no write controls.
 *    - otherwise a write needs an explicit `read_write` grant on that vendor.
 *    - vendor create/rename/delete are admin-only (`require_admin`), regardless
 *      of any per-vendor grant, so they key off the role rather than a level.
 */
import type { MyAccess } from "./types";

export interface Access {
  isAdmin: boolean;
  /** True when the caller may mutate anything under this vendor
   *  (products, sources, extractions). */
  canWriteVendor: (vendorId: string | null | undefined) => boolean;
  /** Vendor CRUD is admin-only, so this is not per-vendor. */
  canManageVendors: boolean;
}

/** Auth disabled on the server, or `/auth/my-access` unavailable: behave as
 *  before this gating existed rather than locking a legitimate admin out of
 *  their own console. The server is still the authority. */
export const OPEN_ACCESS: Access = {
  isAdmin: true,
  canWriteVendor: () => true,
  canManageVendors: true,
};

export function accessFrom(my: MyAccess): Access {
  const isAdmin = my.role === "admin";
  const roleCanWrite = my.role === "admin" || my.role === "read_write";
  const levels = new Map(my.vendors.map((v) => [v.vendor_id, v.level]));
  return {
    isAdmin,
    canManageVendors: isAdmin,
    canWriteVendor: (vendorId) => {
      if (my.see_all) return roleCanWrite;
      if (!vendorId) return false;
      // A grant of read_write still can't exceed the global role.
      return roleCanWrite && levels.get(vendorId) === "read_write";
    },
  };
}
