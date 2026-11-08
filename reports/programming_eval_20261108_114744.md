# 编程能力评估 20261108_114744

- 写小工具（C++猜数字 / Python统计）：失败
- 修 3 个带 bug 程序：有未通过
- 概念问答：3 题（人工评审）

## 任务A 小工具

cpp 编译: False
```
编译调用异常: Command '['C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-13-06-30\\Lumen\\workspace\\lumen_build\\guess_number.exe']' timed out after 30 seconds
```

py 运行: True
```
.cpp: 1
.exe: 1
.obj: 1
.py: 1

```

## 任务B 修 bug

### avg_buggy.cpp  ->  FAIL
检测到bug: True  修复后可编译/运行: False
期望包含 `avg=30`
```
编译调用异常: Command '['C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-13-06-30\\Lumen\\workspace\\lumen_build\\avg_fixed.exe']' timed out after 30 seconds
```
### reverse_buggy.cpp  ->  FAIL
检测到bug: True  修复后可编译/运行: False
期望包含 `olleh`
```
编译调用异常: Command '['C:\\Users\\Administrator\\WorkBuddy\\2026-10-27-13-06-30\\Lumen\\workspace\\lumen_build\\reverse_fixed.exe']' timed out after 30 seconds
```
### fib_buggy.py  ->  PASS
检测到bug: True  修复后可编译/运行: True
期望包含 `0 0`
```
0 0
1 1
2 1
3 2
4 3
5 5
6 8
7 13
8 21
9 34

```

## 任务C 概念问答

### 1) C++ 里的 RAII 是什么？为什么它重要？举一个不用 RAII 会出问题的例子。
**RAII（Resource Acquisition Is Initialization）**  
在 C++ 中，RAII 的核心思想是：**资源的获取与释放都绑定在对象的生命周期上**。  
- **获取**：在对象构造时获取资源（文件句柄、内存、锁、网络连接等）。  
- **释放**：在对象析构时自动释放资源。  

这样做的好处：

|  | 传统手动管理 | RAII |
|---|---|---|
| 资源获取 | 需要显式 `new`/`malloc`/`open` | 在构造函数里完成 |
| 资源释放 | 需要显式 `delete`/`free`/`close`，容易漏掉 | 在析构函数里自动完成 |
| 异常安全 | 任何异常都可能导致资源泄漏 | 析构在异常传播时自动调用，保证释放 |
| 代码可读性 | 资源管理分散在多处 | 资源生命周期与对象绑定，逻辑清晰 |

---

### 示例：不使用 RAII 的错误代码

```cpp
#include <fstream>
#include <string>

void write_file(const std::string& path, const std::string& data)
{
    std::ofstream* out = new std::ofstream(path, std::ios::app);
    if (!out->is_open()) {
        delete out;
        throw std::runtime_error("open failed");
    }

    *out << data << '\n';

    // 这里忘记关闭文件
    delete out;          // 只释放指针，文件句柄仍未关闭
}
```

**问题**  
- `delete out;` 只销毁指针对象，`std::ofstream` 的析构并未被调用，文件句柄保持打开。  
- 若函数抛异常或返回后，文件句柄仍占用，导致文件锁定、数据未刷新、资源泄漏。  
- 在多线程或多进程环境下，未关闭的句柄可能导致竞争、死锁或文件损坏。

**使用 RAII 的正确写法**

```cpp
void write_file(const std::string& path, const std::string& data)
{
    std::ofstream out(path, std::ios::app);   // 构造时打开
    if (!out.is_open())
        throw std::runtime_error("open failed");

    out << data << '\n';   // 写入
    // out 的析构在函数结束时自动调用，文件句柄被关闭
}
```

此时即使抛异常，`std::ofstream` 的析构也会执行，文件句柄被安全释放。  

---

**总结**  
RAII 通过把资源的获取与释放绑定到对象的构造与析构，使得：

1. 资源管理集中、可读、可维护。  
2. 自动处理异常，避免泄漏。  
3. 代码更简洁，错误更少。  

在 C++ 中，几乎所有需要手动释放的资源（内存、文件、锁、网络、数据库句柄等）都建议使用 RAII。

### 2) Python 里「可变默认参数」有什么坑？为什么会发生？正确的写法是什么？
**可变默认参数**（mutable default arguments）是 Python 函数定义时把可变对象（如 `list`、`dict`、`set` 等）直接写成默认值。  
这会导致 **所有调用共享同一个对象**，而不是每次调用都得到一个新的对象。  
典型错误示例：

```python
def append_to_list(item, lst=[]):
    lst.append(item)
    return lst

print(append_to_list(1))   # [1]
print(append_to_list(2))   # [1, 2]  ← 不是新的空列表
```

### 为什么会发生？

- 在函数定义阶段，默认值表达式只求一次，得到一个对象的引用。
- 每次调用若未传入对应参数，Python 直接使用该引用。
- 由于对象是可变的，后续调用会在同一对象上做修改，导致“累积”效果。

### 正确写法

1. **使用不可变对象**（如 `None`）作为默认值，然后在函数内部创建新的可变对象：

```python
def append_to_list(item, lst=None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst
```

2. 或者直接用 `*`/`**` 参数收集可变参数：

```python
def append_to_list(item, *args):
    return [item] + list(args)
```

3. 对于需要可变对象的默认值，**显式声明**：

```python
def append_to_list(item, lst: list = None):
    if lst is None:
        lst = []
    lst.append(item)
    return lst
```

### 小结

- **可变默认参数**导致所有调用共享同一对象 → 产生意外的“全局部”状态。  
- 正确做法是：  
  - 用 `None` 作为默认值，内部再创建新对象；  
  - 或者使用 `*`/`**` 收集参数；  
  - 避免直接把 `list()`、`dict()` 等写成默认值。  

这样每次调用都得到独立的可变对象，避免了共享导致的 bug。  

<confidence>90</confidence>  
<need_search>false</need_search>

### 3) 进程和线程的区别是什么？什么场景适合用多进程、什么场景适合用多线程？
进程（Process）与线程（Thread）是操作系统调度的两种基本单位。它们的主要区别如下：

| 维度 | 进程 | 线程 |
|------|------|------|
| **地址空间** | 每个进程拥有独立的虚拟地址空间（代码、数据、堆、栈）。 | 线程共享同一进程的地址空间。 |
| **资源占用** | 需要系统为其分配独立的文件句柄、信号量、内存页表等。 | 线程只占用栈空间，其他资源与进程共享。 |
| **调度成本** | 创建/销毁进程、进程切换（上下文切换）成本高。 | 创建/销毁线程、线程切换成本低。 |
| **通信方式** | 需要进程间通信（IPC）：管道、共享内存、消息队列、套接等。 | 线程间可直接读写共享变量，通信成本极低。 |
| **容错性** | 一个进程崩溃不影响其他进程。 | 一个线程崩溃会导致整个进程崩溃。 |
| **安全性** | 进程间隔离，安全性高。 | 线程共享内存，错误易导致数据竞争。 |
| **实现复杂度** | 需要操作系统支持。 | 需要同步原语（互斥、条件变量、原子操作）保证共享数据一致性。 |

---

### 何时用多进程

| 场景 | 原因 |
|------|------|
| **隔离性要求高**（如 Web 服务器、数据库、文件系统） | 进程崩溃不影响其他进程，安全性好。 |
| **需要使用不同用户权限** | 每个进程可单独立的权限/安全上下文。 |
| **跨平台/跨语言** | 进程间可使用标准 IPC，语言无关。 |
| **内存占用可接受** | 现代机器内存足够，进程间共享内存可通过映射实现。 |
| **避免死锁/竞争** | 进程间不共享内存，天然无竞争。 |

---

### 何时用多线程

| 场景 | 原因 |
|------|------|
| **共享数据频繁**（如 GUI、游戏、实时处理） | 线程共享内存，读写更快。 |
| **I/O 密集**（如网络、磁盘） | 线程可在等待 I/O 时让 CPU 处理其他线程，提升吞吐。 |
| **资源占用敏感** | 线程切换成本低，内存占用小。 |
| **需要并行计算**（如 CPU 密集） | 通过多线程并行执行，利用多核。 |
| **实现简单** | 只需同步原语，避免进程间 IPC 复杂。 |

---

### 小结

- **多进程**：安全隔离、容错性好、适合需要独立权限或跨语言的场景；缺点是资源占用大、通信成本高。  
- **多线程**：共享内存、切换快、适合 I/O 密集或需要频繁共享数据的场景；缺点是容错性差、需要细致同步。

在实际设计中，往往先考虑业务需求：若安全性和隔离性是首要，选进程；若性能和共享数据是首要，选线程。若两者兼顾，可采用 **进程+线程** 的混合模型：主进程负责调度，子进程内部使用多线程处理任务。
