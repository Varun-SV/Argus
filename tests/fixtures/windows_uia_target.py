"""Tiny Windows UI target used by the hosted-runner safety regression test."""
import sys
import tkinter as tk

root = tk.Tk()
root.title(sys.argv[1] if len(sys.argv) > 1 else "Argus UIA Safety Target")
root.geometry("360x160")

value = tk.StringVar()
entry = tk.Entry(root, textvariable=value, name="argus_entry")
entry.pack(padx=20, pady=(24, 12), fill="x")

status = tk.StringVar(value="idle")
button = tk.Button(root, text="Apply", command=lambda: status.set(value.get()))
button.pack(pady=4)
label = tk.Label(root, textvariable=status)
label.pack(pady=4)

root.mainloop()
