// SPDX-License-Identifier: GPL-2.0
/* a5ctl — GIGABYTE A5 K1 fan + keyboard-RGB control via the \_SB.DCHU _DSM.
 *
 * The firmware exposes fan/RGB control through a Device-Specific Method that
 * needs a Package argument (which userspace acpi_call can't send). This module
 * calls it properly under the ACPI lock. It provides a generic sysfs caller so
 * the exact function map can be verified on-device before wiring a clean UI.
 *
 *   /sys/kernel/a5ctl/call   write:  "<func_hex> [byte_hex]..."  -> evaluate _DSM
 *   /sys/kernel/a5ctl/last   read:   hex bytes returned by the last call
 *
 * DSM: \_SB.DCHU  UUID 93f224e4-fbdc-4bbf-add6-db71bdc0afad  rev 1
 *   func 0x0C read fan telemetry · 0x04 keyboard RGB · 0x68/0x69 fan set (ZEVT)
 */
#include <linux/acpi.h>
#include <linux/kernel.h>
#include <linux/unaligned.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/sysfs.h>

#define DSM_PATH "\\_SB.DCHU"
#define BUFLEN   40

static const guid_t a5_guid =
	GUID_INIT(0x93f224e4, 0xfbdc, 0x4bbf,
		  0xad, 0xd6, 0xdb, 0x71, 0xbd, 0xc0, 0xaf, 0xad);

static acpi_handle dchu;
static DEFINE_MUTEX(a5_lock);
static u8 last_buf[256];
static int last_len;

/* Call the DSM: argv4 = Package{ Buffer(inbuf) }, as ZEVT/PEVT expect. */
static int a5_dsm(u64 func, const u8 *in, size_t in_len)
{
	union acpi_object elem, argv4, *out;
	u8 buf[BUFLEN] = {0};

	if (in_len > BUFLEN)
		in_len = BUFLEN;
	memcpy(buf, in, in_len);

	elem.buffer.type = ACPI_TYPE_BUFFER;
	elem.buffer.length = BUFLEN;
	elem.buffer.pointer = buf;
	argv4.package.type = ACPI_TYPE_PACKAGE;
	argv4.package.count = 1;
	argv4.package.elements = &elem;

	out = acpi_evaluate_dsm(dchu, &a5_guid, 1, func, &argv4);
	if (!out)
		return -EIO;

	last_len = 0;
	if (out->type == ACPI_TYPE_BUFFER) {
		last_len = min_t(int, out->buffer.length, (u32)sizeof(last_buf));
		memcpy(last_buf, out->buffer.pointer, last_len);
	} else if (out->type == ACPI_TYPE_INTEGER) {
		last_len = 4;
		put_unaligned_le32((u32)out->integer.value, last_buf);
	}
	ACPI_FREE(out);
	return 0;
}

static ssize_t call_store(struct kobject *k, struct kobj_attribute *a,
			  const char *in, size_t n)
{
	u8 buf[BUFLEN] = {0};
	unsigned int vals[1 + BUFLEN];
	int cnt, i, ret;
	u64 func;

	/* parse whitespace-separated hex: func then optional data bytes */
	cnt = sscanf(in,
		"%x %x %x %x %x %x %x %x %x %x %x %x %x %x %x %x %x %x %x %x",
		&vals[0], &vals[1], &vals[2], &vals[3], &vals[4], &vals[5],
		&vals[6], &vals[7], &vals[8], &vals[9], &vals[10], &vals[11],
		&vals[12], &vals[13], &vals[14], &vals[15], &vals[16],
		&vals[17], &vals[18], &vals[19]);
	if (cnt < 1)
		return -EINVAL;
	func = vals[0];
	for (i = 1; i < cnt; i++)
		buf[i - 1] = vals[i] & 0xFF;

	mutex_lock(&a5_lock);
	ret = a5_dsm(func, buf, cnt - 1);
	mutex_unlock(&a5_lock);
	return ret ? ret : n;
}

static ssize_t last_show(struct kobject *k, struct kobj_attribute *a, char *out)
{
	int i, p = 0;

	mutex_lock(&a5_lock);
	for (i = 0; i < last_len; i++)
		p += scnprintf(out + p, PAGE_SIZE - p, "%02x ", last_buf[i]);
	mutex_unlock(&a5_lock);
	p += scnprintf(out + p, PAGE_SIZE - p, "\n");
	return p;
}

static struct kobj_attribute call_attr = __ATTR(call, 0200, NULL, call_store);
static struct kobj_attribute last_attr = __ATTR(last, 0444, last_show, NULL);
static struct attribute *a5_attrs[] = { &call_attr.attr, &last_attr.attr, NULL };
static const struct attribute_group a5_grp = { .attrs = a5_attrs };
static struct kobject *a5_kobj;

static int __init a5_init(void)
{
	if (ACPI_FAILURE(acpi_get_handle(NULL, DSM_PATH, &dchu))) {
		pr_err("a5ctl: %s not found\n", DSM_PATH);
		return -ENODEV;
	}
	if (!acpi_check_dsm(dchu, &a5_guid, 1, 0xFFFFFFFF))
		pr_warn("a5ctl: _DSM present but function query odd; continuing\n");

	a5_kobj = kobject_create_and_add("a5ctl", kernel_kobj);
	if (!a5_kobj)
		return -ENOMEM;
	if (sysfs_create_group(a5_kobj, &a5_grp)) {
		kobject_put(a5_kobj);
		return -ENOMEM;
	}
	pr_info("a5ctl: ready (\\_SB.DCHU _DSM)\n");
	return 0;
}

static void __exit a5_exit(void)
{
	sysfs_remove_group(a5_kobj, &a5_grp);
	kobject_put(a5_kobj);
}

module_init(a5_init);
module_exit(a5_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("GIGABYTE A5 K1 fan + keyboard RGB via _SB.DCHU _DSM");
MODULE_AUTHOR("leon + Claude");
