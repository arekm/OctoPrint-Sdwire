#include <fcntl.h>
#include <linux/msdos_fs.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <string.h>

#include <Python.h>

/* Returns short vfat filename for specified directory and long filename */
static PyObject *vfat_get_short_name(PyObject *self, PyObject *args) {
	char *dir = NULL, *long_filename = NULL;
	int fd, ret;
	struct __fat_dirent entry[2];

	/* Parse arguments */
	if(!PyArg_ParseTuple(args, "ss", &dir, &long_filename)) {
		return NULL;
	}

	fd = open(dir, O_RDONLY | O_DIRECTORY);
	if (fd == -1) {
		PyErr_SetFromErrnoWithFilename(PyExc_OSError, dir);
		return NULL;
	}

	for (;;) {
		ret = ioctl( fd, VFAT_IOCTL_READDIR_BOTH, entry);
		if (ret < 0) {
			PyErr_SetFromErrnoWithFilename(PyExc_OSError, dir);
			close(fd);
			return NULL;
		}
		if (ret == 0)
			break;

		if (strcasecmp(entry[1].d_name, long_filename) == 0) {
			/* printf("%s -> '%s'\n", entry[0].d_name, entry[1].d_name); */
			close(fd);
			return PyBytes_FromString(entry[0].d_name);
		}
	}

	close(fd);

	Py_INCREF(Py_None);
	return Py_None;
}

static PyMethodDef vfatMethods[] = {
	{"get_short_name",  vfat_get_short_name, METH_VARARGS,
		"Get short vfat file name for long filename in specified dir."},
	{NULL, NULL, 0, NULL}
};


static struct PyModuleDef vfatmodule = {
	PyModuleDef_HEAD_INIT,
	"_vfatdir",	/* name of module */
	NULL,		/* module documentation, may be NULL */
	-1,		/* size of per-interpreter state of the module,
			   or -1 if the module keeps state in global variables. */
	vfatMethods
};

PyMODINIT_FUNC
PyInit__vfatdir(void)
{
	return PyModule_Create(&vfatmodule);
}
