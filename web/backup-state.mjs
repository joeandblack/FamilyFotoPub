// The retained modal owns requests until navigation returns to the restarted app.
export function backupActive(root=globalThis.document){
 return Boolean(root?.querySelector?.('dialog.backup-progress-dialog[open]'));
}
