/**
 * PyOS NOVA — UEFI EFI Loader
 * ============================
 * Minimal UEFI application that boots CPython directly.
 * This is the ONLY non-Python code in PyOS NOVA.
 * Its entire job: ask UEFI "where is my partition?" → launch Python.
 *
 * Compiled as a PE32+ EFI application with no libc, no OS, no kernel.
 * Size target: < 8 KB compiled.
 *
 * Boot sequence:
 *   UEFI firmware → BOOTX64.EFI (this file) → python3 /nova/boot/pyinit.py
 *
 * What this does NOT do:
 *   - No memory management beyond what UEFI provides
 *   - No device drivers
 *   - No filesystem formatting
 *   - No process management
 *   All of that is Python's job (kernel/nova.py)
 *
 * UEFI services used:
 *   - EFI_LOADED_IMAGE_PROTOCOL   (find our partition)
 *   - EFI_SIMPLE_FILE_SYSTEM_PROTOCOL (read files)
 *   - ConOut->OutputString         (print boot messages)
 *   - ExitBootServices             (hand control to Python)
 *     NOTE: We do NOT call ExitBootServices — Python decides when
 *     it's done with UEFI services.
 */

/* ── Minimal UEFI type definitions ─────────────────────────────────────────── */
typedef unsigned char      UINT8;
typedef unsigned short     UINT16;
typedef unsigned int       UINT32;
typedef unsigned long long UINT64;
typedef long long          INT64;
typedef unsigned long      UINTN;
typedef void*              VOID;
typedef UINT16             CHAR16;    /* UTF-16LE */
typedef UINT64             EFI_STATUS;
typedef void*              EFI_HANDLE;
typedef void*              EFI_EVENT;
typedef UINT64             EFI_LBA;
typedef UINT64             EFI_PHYSICAL_ADDRESS;
typedef UINT64             EFI_VIRTUAL_ADDRESS;

#define EFI_SUCCESS         0ULL
#define EFI_NOT_FOUND       (0x8000000000000000ULL | 14)
#define EFI_LOAD_ERROR      (0x8000000000000000ULL |  1)
#define NULL                ((void*)0)
#define TRUE                1
#define FALSE               0

/* UEFI GUID (16 bytes) */
typedef struct {
    UINT32 Data1;
    UINT16 Data2;
    UINT16 Data3;
    UINT8  Data4[8];
} EFI_GUID;

/* ── EFI Table Header ───────────────────────────────────────────────────────── */
typedef struct {
    UINT64 Signature;
    UINT32 Revision;
    UINT32 HeaderSize;
    UINT32 CRC32;
    UINT32 Reserved;
} EFI_TABLE_HEADER;

/* ── Simple Text Output (console) ─────────────────────────────────────────── */
typedef struct EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL {
    void*      Reset;
    EFI_STATUS (*OutputString)(struct EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL*, CHAR16*);
    void*      TestString;
    void*      QueryMode;
    void*      SetMode;
    void*      SetAttribute;
    EFI_STATUS (*ClearScreen)(struct EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL*);
    void*      SetCursorPosition;
    void*      EnableCursor;
    void*      Mode;
} EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL;

/* ── Boot Services (subset) ──────────────────────────────────────────────── */
typedef struct {
    EFI_TABLE_HEADER Hdr;
    void* RaiseTPL;
    void* RestoreTPL;
    void* AllocatePages;
    void* FreePages;
    void* GetMemoryMap;
    EFI_STATUS (*AllocatePool)(UINT32 PoolType, UINTN Size, VOID** Buffer);
    EFI_STATUS (*FreePool)(VOID* Buffer);
    void* CreateEvent;
    void* SetTimer;
    void* WaitForEvent;
    void* SignalEvent;
    void* CloseEvent;
    void* CheckEvent;
    void* InstallProtocolInterface;
    void* ReinstallProtocolInterface;
    void* UninstallProtocolInterface;
    EFI_STATUS (*HandleProtocol)(EFI_HANDLE Handle, EFI_GUID* Protocol, VOID** Interface);
    void* Reserved;
    void* RegisterProtocolNotify;
    EFI_STATUS (*LocateHandle)(UINT32 SearchType, EFI_GUID* Protocol, VOID* SearchKey,
                               UINTN* BufferSize, EFI_HANDLE* Buffer);
    void* LocateDevicePath;
    void* InstallConfigurationTable;
    EFI_STATUS (*LoadImage)(UINT8 BootPolicy, EFI_HANDLE ParentHandle,
                            void* DevicePath, VOID* SourceBuffer, UINTN SourceSize,
                            EFI_HANDLE* ImageHandle);
    EFI_STATUS (*StartImage)(EFI_HANDLE ImageHandle, UINTN* ExitDataSize, CHAR16** ExitData);
    void* Exit;
    void* UnloadImage;
    void* ExitBootServices;
} EFI_BOOT_SERVICES;

/* ── System Table ─────────────────────────────────────────────────────────── */
typedef struct {
    EFI_TABLE_HEADER              Hdr;
    CHAR16*                       FirmwareVendor;
    UINT32                        FirmwareRevision;
    EFI_HANDLE                    ConsoleInHandle;
    void*                         ConIn;
    EFI_HANDLE                    ConsoleOutHandle;
    EFI_SIMPLE_TEXT_OUTPUT_PROTOCOL* ConOut;
    EFI_HANDLE                    StandardErrorHandle;
    void*                         StdErr;
    void*                         RuntimeServices;
    EFI_BOOT_SERVICES*            BootServices;
    UINTN                         NumberOfTableEntries;
    void*                         ConfigurationTable;
} EFI_SYSTEM_TABLE;

/* ── Loaded Image Protocol ────────────────────────────────────────────────── */
#define EFI_LOADED_IMAGE_PROTOCOL_GUID \
    {0x5B1B31A1,0x9562,0x11D2,{0x8E,0x3F,0x00,0xA0,0xC9,0x69,0x72,0x3B}}

typedef struct {
    UINT32     Revision;
    EFI_HANDLE ParentHandle;
    EFI_SYSTEM_TABLE* SystemTable;
    EFI_HANDLE DeviceHandle;
    void*      FilePath;
    VOID*      Reserved;
    UINT32     LoadOptionsSize;
    VOID*      LoadOptions;
    VOID*      ImageBase;
    UINT64     ImageSize;
    UINT32     ImageCodeType;
    UINT32     ImageDataType;
    void*      Unload;
} EFI_LOADED_IMAGE_PROTOCOL;

/* ── Simple File System Protocol ──────────────────────────────────────────── */
#define EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID \
    {0x0964E5B22,0x6459,0x11D2,{0x8E,0x39,0x00,0xA0,0xC9,0x69,0x72,0x3B}}

typedef struct EFI_FILE_PROTOCOL {
    UINT64 Revision;
    EFI_STATUS (*Open)(struct EFI_FILE_PROTOCOL* This,
                       struct EFI_FILE_PROTOCOL** NewHandle,
                       CHAR16* FileName, UINT64 OpenMode, UINT64 Attributes);
    EFI_STATUS (*Close)(struct EFI_FILE_PROTOCOL* This);
    EFI_STATUS (*Delete)(struct EFI_FILE_PROTOCOL* This);
    EFI_STATUS (*Read)(struct EFI_FILE_PROTOCOL* This, UINTN* BufferSize, VOID* Buffer);
    EFI_STATUS (*Write)(struct EFI_FILE_PROTOCOL* This, UINTN* BufferSize, VOID* Buffer);
    EFI_STATUS (*GetPosition)(struct EFI_FILE_PROTOCOL* This, UINT64* Position);
    EFI_STATUS (*SetPosition)(struct EFI_FILE_PROTOCOL* This, UINT64 Position);
    EFI_STATUS (*GetInfo)(struct EFI_FILE_PROTOCOL* This, EFI_GUID* InfoType,
                          UINTN* BufferSize, VOID* Buffer);
    void* SetInfo;
    void* Flush;
} EFI_FILE_PROTOCOL;

typedef struct {
    UINT64 Revision;
    EFI_STATUS (*OpenVolume)(struct _EFI_SIMPLE_FILE_SYSTEM_PROTOCOL* This,
                              EFI_FILE_PROTOCOL** Root);
} EFI_SIMPLE_FILE_SYSTEM_PROTOCOL;

/* ── Open mode flags ──────────────────────────────────────────────────────── */
#define EFI_FILE_MODE_READ   0x0000000000000001ULL
#define EFI_FILE_MODE_WRITE  0x0000000000000002ULL
#define EFI_FILE_READ_ONLY   0x0000000000000001ULL

/* ── Memory types ─────────────────────────────────────────────────────────── */
#define EfiLoaderData 2

/* ── Globals ─────────────────────────────────────────────────────────────── */
static EFI_SYSTEM_TABLE*   gST  = NULL;
static EFI_BOOT_SERVICES*  gBS  = NULL;
static EFI_HANDLE           gImageHandle = NULL;

/* ── String helpers (no libc) ─────────────────────────────────────────────── */

static void print16(CHAR16* s) {
    if (gST && gST->ConOut)
        gST->ConOut->OutputString(gST->ConOut, s);
}

/* Compute length of CHAR16 string */
static UINTN str16len(CHAR16* s) {
    UINTN n = 0;
    while (s[n]) n++;
    return n;
}

/* Copy ASCII string to CHAR16 (for string literals) */
static void ascii_to_utf16(const char* src, CHAR16* dst, UINTN max) {
    UINTN i = 0;
    while (src[i] && i < max - 1) {
        dst[i] = (CHAR16)(unsigned char)src[i];
        i++;
    }
    dst[i] = 0;
}

/* ── Pool allocation helper ───────────────────────────────────────────────── */
static VOID* alloc(UINTN size) {
    VOID* buf = NULL;
    gBS->AllocatePool(EfiLoaderData, size, &buf);
    return buf;
}

/* ── Read a file from the EFI System Partition ───────────────────────────── */
static EFI_STATUS
read_file(EFI_FILE_PROTOCOL* root, CHAR16* path,
          VOID** data_out, UINTN* size_out) {

    EFI_FILE_PROTOCOL* fh = NULL;
    EFI_STATUS status;

    status = root->Open(root, &fh, path,
                        EFI_FILE_MODE_READ, EFI_FILE_READ_ONLY);
    if (status != EFI_SUCCESS) return status;

    /* Get file size by seeking to end */
    status = fh->SetPosition(fh, 0xFFFFFFFFFFFFFFFFULL);
    if (status != EFI_SUCCESS) { fh->Close(fh); return status; }

    UINT64 size = 0;
    status = fh->GetPosition(fh, &size);
    if (status != EFI_SUCCESS) { fh->Close(fh); return status; }

    status = fh->SetPosition(fh, 0);
    if (status != EFI_SUCCESS) { fh->Close(fh); return status; }

    /* Allocate buffer and read */
    VOID* buf = alloc(size + 2);   /* +2 for null terminator */
    if (!buf) { fh->Close(fh); return EFI_LOAD_ERROR; }

    UINTN read_size = (UINTN)size;
    status = fh->Read(fh, &read_size, buf);
    fh->Close(fh);

    if (status == EFI_SUCCESS) {
        ((UINT8*)buf)[size]   = 0;   /* null-terminate */
        ((UINT8*)buf)[size+1] = 0;
        *data_out = buf;
        *size_out = read_size;
    }
    return status;
}

/* ── Find the root of our EFI partition ───────────────────────────────────── */
static EFI_STATUS
open_root_fs(EFI_FILE_PROTOCOL** root_out) {

    EFI_GUID lip_guid = EFI_LOADED_IMAGE_PROTOCOL_GUID;
    EFI_GUID sfs_guid = EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID;

    /* Get our loaded image info to find our device handle */
    EFI_LOADED_IMAGE_PROTOCOL* lip = NULL;
    EFI_STATUS status = gBS->HandleProtocol(gImageHandle, &lip_guid,
                                             (VOID**)&lip);
    if (status != EFI_SUCCESS) return status;

    /* Open the Simple Filesystem on our device */
    EFI_SIMPLE_FILE_SYSTEM_PROTOCOL* sfs = NULL;
    status = gBS->HandleProtocol(lip->DeviceHandle, &sfs_guid, (VOID**)&sfs);
    if (status != EFI_SUCCESS) return status;

    return sfs->OpenVolume(sfs, root_out);
}

/* ── Launch a PE32+ EFI image from the filesystem ────────────────────────── */
static EFI_STATUS
launch_image(EFI_FILE_PROTOCOL* root, CHAR16* path) {

    VOID*  data      = NULL;
    UINTN  data_size = 0;
    EFI_STATUS status;

    status = read_file(root, path, &data, &data_size);
    if (status != EFI_SUCCESS) return status;

    EFI_HANDLE new_handle = NULL;
    status = gBS->LoadImage(FALSE, gImageHandle, NULL,
                             data, data_size, &new_handle);
    if (status != EFI_SUCCESS) {
        gBS->FreePool(data);
        return status;
    }

    UINTN exit_data_size = 0;
    status = gBS->StartImage(new_handle, &exit_data_size, NULL);
    gBS->FreePool(data);
    return status;
}

/* ══════════════════════════════════════════════════════════════════════════
 * EFI entry point
 * Called by UEFI firmware with:
 *   ImageHandle — handle to our loaded image
 *   SystemTable — pointer to EFI System Table (all services live here)
 * ══════════════════════════════════════════════════════════════════════════ */
EFI_STATUS
efi_main(EFI_HANDLE ImageHandle, EFI_SYSTEM_TABLE* SystemTable) {

    gST          = SystemTable;
    gBS          = SystemTable->BootServices;
    gImageHandle = ImageHandle;

    /* Clear screen and print banner */
    gST->ConOut->ClearScreen(gST->ConOut);
    print16(L"\r\n"
            L"  \033[36m╔═══════════════════════════════════════╗\033[0m\r\n"
            L"  \033[36m║       PyOS NOVA  v0.0008              ║\033[0m\r\n"
            L"  \033[36m║   Python is the operating system      ║\033[0m\r\n"
            L"  \033[36m╚═══════════════════════════════════════╝\033[0m\r\n"
            L"\r\n");

    /* Open the EFI System Partition root */
    EFI_FILE_PROTOCOL* root = NULL;
    EFI_STATUS status = open_root_fs(&root);
    if (status != EFI_SUCCESS) {
        print16(L"  \033[31m[FAIL]\033[0m Cannot open EFI partition\r\n");
        return status;
    }
    print16(L"  \033[32m[  OK]\033[0m EFI partition mounted\r\n");

    /*
     * Try to launch the Python EFI application.
     * Search order:
     *   1. /python/python3.efi    — CPython compiled as UEFI app (full)
     *   2. /python/micropython.efi — MicroPython UEFI app (lightweight)
     *   3. /python/nova_python.efi — Our embedded Python build
     */
    CHAR16* python_candidates[] = {
        L"\\python\\python3.efi",
        L"\\python\\micropython.efi",
        L"\\python\\nova_python.efi",
        NULL,
    };

    for (int i = 0; python_candidates[i] != NULL; i++) {
        print16(L"  Trying: ");
        print16(python_candidates[i]);
        print16(L"\r\n");

        status = launch_image(root, python_candidates[i]);
        if (status == EFI_SUCCESS) {
            /* Python exited cleanly */
            print16(L"\r\n  \033[36mPyOS NOVA exited.\033[0m\r\n");
            return EFI_SUCCESS;
        }
    }

    /*
     * Python EFI binary not found.
     * Print installation instructions.
     */
    print16(L"\r\n"
            L"  \033[33m[WARN]\033[0m Python EFI application not found.\r\n"
            L"\r\n"
            L"  To build the Python EFI binary, run on a Linux machine:\r\n"
            L"\r\n"
            L"    python3 build/usb_image.py --build-python-efi\r\n"
            L"\r\n"
            L"  Or use the Docker builder:\r\n"
            L"\r\n"
            L"    docker build -t nova-builder -f Dockerfile.build .\r\n"
            L"    docker run --rm -v $(pwd):/workspace nova-builder\r\n"
            L"\r\n"
            L"  Then copy python3.efi to /python/ on the EFI partition.\r\n"
            L"\r\n");

    /* Hang so user can read the message */
    volatile int spin = 1;
    while (spin) { /* wait for user to power off */ }

    return EFI_NOT_FOUND;
}
