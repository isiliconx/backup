# Recovering a replacement VM

For backups created without password protection, leave the import password empty or add `--no-password` to the CLI recovery commands below. Protected backups still require their original password.

Keep the complete `.vmbackup` file and its password outside the VM. Keep your encryption recovery keys, matching OS installer/rescue media and access to the VM provider separately. This tool cannot create a replacement VM or change its firmware, virtual TPM or host settings.

## Ordinary files

Install GuestVault on the same operating system. Run `setup`, then use Import & restore or the CLI:

```sh
guestvault verify /downloaded/backup.vmbackup
guestvault restore /downloaded/backup.vmbackup --target /empty/recovered
```

Paths retain their original hierarchy. For example, `/home/alice/.config` is restored beneath `/empty/recovered/home/alice/.config`. Restic represents Windows paths in its snapshot hierarchy; inspect the restored tree before moving files to their original drive locations. Stop the relevant applications first. Recover account/keyring configuration along with browser profiles if you need encrypted stored credentials. Hardware-bound credentials and server-side sessions may not survive a machine change.

## Linux system files

This workflow restores saved OS/application files. It is not sector-identical recovery and does not restore running processes. It requires an offline target with compatible architecture and enough disk space.

1. Boot the replacement VM into a Linux rescue/live ISO. Install GuestVault in that environment. Put its state, imported archive and temporary work on a separate recovery/storage volume with enough space; do not place them on the disk being restored.
2. Inspect the original layout recorded in the encrypted recovery metadata and the new layout using `lsblk -f`. Restore the file bundle first into a temporary empty tree if you need to inspect that metadata before preparing the final disk. Recreate appropriate filesystems and mount their root at a path such as `/mnt/recovered`. Use the same filesystem UUIDs where appropriate, or update the restored `/etc/fstab` and encryption/mount settings. GuestVault does not format or repartition disks for file recovery.
3. Mount any separate `/boot`, EFI, `/home` and other volumes at their matching paths under the target root. The target's directory tree must be empty for GuestVault's safe file import. For complex multi-filesystem layouts, first restore to an empty staging tree, then move/copy each subtree to its final mount using a tool that preserves ownership, hardlinks and all extended metadata (for example `rsync -aHAX --numeric-ids`). An empty newly formatted ext filesystem may have an empty `lost+found` directory; remove that empty directory with `rmdir` if needed, rather than bypassing the restore guard.
4. With the destination offline and empty, run as root:

   ```sh
   guestvault --state /recovery-storage/guestvault restore /recovery-storage/backup.vmbackup --target /mnt/recovered
   ```

5. Review the restored recovery JSON (under the original state directory's `recovery/current/recovery.json`), `/etc/fstab`, network configuration and initramfs requirements. Create the excluded runtime mount directories if needed. For a conventional GRUB-based distro, bind-mount `/dev`, `/proc`, `/sys` and `/run` into the restored tree, chroot, regenerate the initramfs and install/update GRUB using that distribution's recovery instructions. EFI installations need the correct mounted EFI System Partition and firmware mode. Cloud images or externally booted filesystems may have provider-specific boot recovery. This tool deliberately does not guess the target disk or execute a generic bootloader command.
6. Unmount the target cleanly and boot it. Check accounts, application data, services and browser/keyring access. Start databases only after validating their consistency.

For layout/boot-sector preservation, use the offline whole-disk mode below instead of a live file backup. Btrfs subvolume structure and filesystem-specific flags are not reconstructed by file imports; prepare them explicitly or use a whole-disk image.

## Windows system image

Run GuestVault elevated inside Windows, with `wbadmin` available and a separate writable NTFS backup drive. The native backup includes the system drive and Windows critical volumes; additional noncritical data drives need their own file backup. GuestVault does not provision the image staging disk.

```powershell
guestvault configure --mode windows-image --image-target E: --repository F:\EncryptedRepo --export-directory F:\Portable --every-hours 24 --schedule --remember-password
guestvault backup --use-saved-password
```

On the replacement Windows VM or recovery helper, import the `.vmbackup` to an empty staging directory with administrative privileges. Locate the recovered `WindowsImageBackup` directory in the original drive-path hierarchy. Place it at the root of an appropriate recovery drive, preserving its full structure. Boot Windows installer/recovery media and use **System Image Recovery**. Match firmware mode and provide the original BitLocker recovery key if required.

Ordinary Windows VSS file mode does not substitute for this system-image workflow. Registry, critical partitions, boot configuration and application installation are why native image recovery exists. `wbadmin` availability and supported features depend on the Windows edition. See Microsoft's [wbadmin start backup reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/wbadmin-start-backup).

## macOS

Enable Full Disk Access for the terminal/Python process, run system file backup as root and stop apps that write their data. GuestVault saves accessible user and application data, not the sealed system volume or VM firmware.

After a wipe, install a compatible macOS release using the VM provider's supported mechanism. Restore files to an empty folder with Full Disk Access and the appropriate permissions. Reinstall applications as required and recover their data. Automatic Time Machine/Migration Assistant integration is not implemented by this release. Keychains can require original account passwords; Secure Enclave/TPM-bound material cannot be reconstructed from guest files. See Apple's [Mac recovery guidance](https://support.apple.com/guide/mac-help/recover-all-your-files-mh15638/mac).

## Offline whole-disk images

This captures all readable disk sectors, including partition/boot/filesystem data, from **inside a Linux rescue VM environment**. The disk must be offline. You can use a Linux rescue ISO to capture a Windows/macOS guest disk, subject to the VM provider permitting access. It does not capture running RAM or external virtual hardware identity and cannot run periodically against a mounted live system disk.

Boot rescue media, unmount every partition on the original whole disk, deactivate swap on it, and check `lsblk` carefully. Use separate storage for the repository, export, GuestVault state and temporary work.

```sh
# /dev/vda is only an example; identify your actual offline whole disk first.
sudo guestvault --state /recovery-storage/guestvault backup-disk \
  --device /dev/vda --repository /recovery-storage/image-repository \
  --bundle /recovery-storage/disk.vmbackup
```

Restoring a disk image is destructive. Create a replacement virtual disk with **exactly the original byte size**, boot Linux rescue media and keep that disk unmounted. The repeated `--erase-device` path explicitly authorizes erasing the named target:

```sh
sudo guestvault --state /recovery-storage/guestvault restore-disk \
  /recovery-storage/disk.vmbackup --device /dev/vda --erase-device /dev/vda
```

The tool verifies the encrypted archive and saved image size before writing, refuses mounted disks/partitions and swap, opens the destination exclusively, writes all sectors, flushes the writes and reads them back to compare SHA-256 checksums. Interrupted/failed disk restoration is reported as failure; keep rescue media booted and restore again. After successful recovery, shut down rescue media and boot the restored disk with compatible original VM firmware/hardware.

For a VM with multiple disks, capture and restore each one from the same offline point. Keep host/provider configuration and virtual TPM recovery outside this tool. Even identical disk contents cannot guarantee that remote websites retain logins.
