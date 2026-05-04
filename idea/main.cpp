#include <iostream>
#include <fstream>
#include <vector>
#include <cstring>
#include <cstdlib>

using namespace std;

// 52 подключа шифрования и дешифрования
unsigned int encKeys[52];
unsigned int decKeys[52];

// Умножение по модулю (2^16 + 1), 0 трактуется как 2^16
unsigned int mulMod(unsigned int a, unsigned int b) {
    unsigned long long ua = (a == 0) ? 65536 : a;
    unsigned long long ub = (b == 0) ? 65536 : b;
    unsigned long long r = (ua * ub) % 65537;
    return (unsigned int)(r & 0xFFFF);
}

// Сложение по модулю 2^16
unsigned int addMod(unsigned int a, unsigned int b) {
    return (a + b) & 0xFFFF;
}

// Мультипликативная инверсия по модулю 2^16 + 1 (расширенный алгоритм Евклида)
unsigned int mulInv(unsigned int a) {
    if (a <= 1) return a;

    int t0 = 0, t1 = 1;
    int r0 = 65537, r1 = a;

    while (r1 != 0) {
        int q = r0 / r1;
        int tmp;

        tmp = r0 - q * r1;
        r0 = r1;
        r1 = tmp;

        tmp = t0 - q * t1;
        t0 = t1;
        t1 = tmp;
    }

    if (t0 < 0)
        t0 += 65537;

    return (unsigned int)t0;
}

// Аддитивная инверсия по модулю 2^16
unsigned int addInv(unsigned int a) {
    return (65536 - a) & 0xFFFF;
}

// Генерация 52 подключей шифрования из 128-битного ключа
void generateEncKeys(unsigned char key[16]) {
    unsigned char k[16];
    memcpy(k, key, 16);

    int idx = 0;
    while (idx < 52) {
        // берем 8 подключей из текущего состояния ключа
        for (int i = 0; i < 8 && idx < 52; i++, idx++) {
            encKeys[idx] = (k[2 * i] << 8) | k[2 * i + 1];
        }

        // циклический сдвиг 128-битного ключа на 25 бит влево
        if (idx < 52) {
            unsigned char tmp[16];
            for (int i = 0; i < 16; i++) {
                tmp[i] = (k[(i + 3) % 16] << 1) | (k[(i + 4) % 16] >> 7);
            }
            memcpy(k, tmp, 16);
        }
    }
}

// Генерация подключей дешифрования на основе подключей шифрования
void generateDecKeys() {
    // 9-й раунд (выходное преобразование)
    decKeys[48] = mulInv(encKeys[0]);
    decKeys[49] = addInv(encKeys[1]);
    decKeys[50] = addInv(encKeys[2]);
    decKeys[51] = mulInv(encKeys[3]);

    // 1-й раунд дешифрования
    decKeys[0] = mulInv(encKeys[48]);
    decKeys[1] = addInv(encKeys[49]);
    decKeys[2] = addInv(encKeys[50]);
    decKeys[3] = mulInv(encKeys[51]);
    decKeys[4] = encKeys[46];
    decKeys[5] = encKeys[47];

    // раунды 2..8 дешифрования (средние ключи меняются местами)
    for (int r = 2; r <= 8; r++) {
        int ei = (9 - r) * 6; // индекс в encKeys
        int di = (r - 1) * 6; // индекс в decKeys

        decKeys[di + 0] = mulInv(encKeys[ei + 0]);
        decKeys[di + 1] = addInv(encKeys[ei + 2]); // меняем местами
        decKeys[di + 2] = addInv(encKeys[ei + 1]); // меняем местами
        decKeys[di + 3] = mulInv(encKeys[ei + 3]);
        decKeys[di + 4] = encKeys[ei - 2];
        decKeys[di + 5] = encKeys[ei - 1];
    }
}

// Шифрование/дешифрование одного 64-битного блока
void ideaBlock(unsigned char in[8], unsigned char out[8], unsigned int keys[52]) {
    unsigned int x1 = (in[0] << 8) | in[1];
    unsigned int x2 = (in[2] << 8) | in[3];
    unsigned int x3 = (in[4] << 8) | in[5];
    unsigned int x4 = (in[6] << 8) | in[7];

    // 8 основных раундов
    for (int r = 0; r < 8; r++) {
        int i = r * 6;

        x1 = mulMod(x1, keys[i + 0]);
        x2 = addMod(x2, keys[i + 1]);
        x3 = addMod(x3, keys[i + 2]);
        x4 = mulMod(x4, keys[i + 3]);

        // мультипликативно-аддитивная структура
        unsigned int t1 = x1 ^ x3;
        unsigned int t2 = x2 ^ x4;

        t1 = mulMod(t1, keys[i + 4]);
        t2 = addMod(t1, t2);
        t2 = mulMod(t2, keys[i + 5]);
        t1 = addMod(t1, t2);

        x1 = x1 ^ t2;
        x3 = x3 ^ t2;
        x2 = x2 ^ t1;
        x4 = x4 ^ t1;

        // перестановка средних подблоков
        unsigned int tmp = x2;
        x2 = x3;
        x3 = tmp;
    }

    // 9-й раунд (выходное преобразование)
    x1 = mulMod(x1, keys[48]);
    unsigned int tmp = addMod(x3, keys[49]);
    x3 = addMod(x2, keys[50]);
    x2 = tmp;
    x4 = mulMod(x4, keys[51]);

    out[0] = (x1 >> 8) & 0xFF;
    out[1] = x1 & 0xFF;
    out[2] = (x2 >> 8) & 0xFF;
    out[3] = x2 & 0xFF;
    out[4] = (x3 >> 8) & 0xFF;
    out[5] = x3 & 0xFF;
    out[6] = (x4 >> 8) & 0xFF;
    out[7] = x4 & 0xFF;
}

// Чтение ключа из бинарного файла (16 байт)
bool readKey(const char* filename, unsigned char key[16]) {
    ifstream f(filename, ios::binary);
    if (!f) {
        cerr << "Ошибка: не удалось открыть файл ключа " << filename << endl;
        return false;
    }
    f.read((char*)key, 16);
    if (f.gcount() != 16) {
        cerr << "Ошибка: файл ключа должен быть ровно 16 байт" << endl;
        return false;
    }
    f.close();
    return true;
}

// Шифрование файла
void encryptFile(const char* inputFile, const char* keyFile, const char* outputFile) {
    unsigned char key[16];
    if (!readKey(keyFile, key)) return;

    generateEncKeys(key);

    // чтение входного файла
    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Ошибка: не удалось открыть входной файл " << inputFile << endl;
        return;
    }

    // читаем весь файл в память
    fin.seekg(0, ios::end);
    long long fileSize = fin.tellg();
    fin.seekg(0, ios::beg);

    vector<unsigned char> data(fileSize);
    fin.read((char*)data.data(), fileSize);
    fin.close();

    // дополняем нулями до кратности 8
    int padding = 0;
    if (fileSize % 8 != 0)
        padding = 8 - (fileSize % 8);
    for (int i = 0; i < padding; i++)
        data.push_back(0);

    // открываем выходной файл
    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Ошибка: не удалось открыть выходной файл " << outputFile << endl;
        return;
    }

    // записываем размер исходного файла (8 байт, big-endian)
    unsigned char sizeBytes[8];
    unsigned long long sz = fileSize;
    for (int i = 7; i >= 0; i--) {
        sizeBytes[i] = sz & 0xFF;
        sz >>= 8;
    }
    fout.write((char*)sizeBytes, 8);

    // шифруем блоками по 8 байт
    unsigned char block[8], enc[8];
    for (int pos = 0; pos < (int)data.size(); pos += 8) {
        for (int j = 0; j < 8; j++)
            block[j] = data[pos + j];

        ideaBlock(block, enc, encKeys);
        fout.write((char*)enc, 8);
    }

    fout.close();
    cout << "Шифрование завершено. Результат записан в " << outputFile << endl;
}

// Дешифрование файла
void decryptFile(const char* inputFile, const char* keyFile, const char* outputFile) {
    unsigned char key[16];
    if (!readKey(keyFile, key)) return;

    generateEncKeys(key);
    generateDecKeys();

    // чтение зашифрованного файла
    ifstream fin(inputFile, ios::binary);
    if (!fin) {
        cerr << "Ошибка: не удалось открыть входной файл " << inputFile << endl;
        return;
    }

    // первые 8 байт — размер оригинального файла
    unsigned char sizeBytes[8];
    fin.read((char*)sizeBytes, 8);
    unsigned long long origSize = 0;
    for (int i = 0; i < 8; i++) {
        origSize = (origSize << 8) | sizeBytes[i];
    }

    // читаем остальные данные
    fin.seekg(0, ios::end);
    long long totalSize = fin.tellg();
    long long dataSize = totalSize - 8;
    fin.seekg(8, ios::beg);

    vector<unsigned char> data(dataSize);
    fin.read((char*)data.data(), dataSize);
    fin.close();

    // открываем выходной файл
    ofstream fout(outputFile, ios::binary);
    if (!fout) {
        cerr << "Ошибка: не удалось открыть выходной файл " << outputFile << endl;
        return;
    }

    // дешифруем блоками по 8 байт
    unsigned char block[8], dec[8];
    long long written = 0;

    for (int pos = 0; pos < (int)data.size(); pos += 8) {
        for (int j = 0; j < 8; j++)
            block[j] = data[pos + j];

        ideaBlock(block, dec, decKeys);

        // не записываем лишние байты паддинга
        int toWrite = 8;
        if (written + 8 > (long long)origSize)
            toWrite = (int)(origSize - written);

        fout.write((char*)dec, toWrite);
        written += toWrite;
    }

    fout.close();
    cout << "Дешифрование завершено. Результат записан в " << outputFile << endl;
}

int main(int argc, char* argv[]) {
    if (argc != 5) {
        cout << "Использование:" << endl;
        cout << "  Шифрование:   " << argv[0] << " encrypt <входной_файл> <файл_ключа> <выходной_файл>" << endl;
        cout << "  Дешифрование: " << argv[0] << " decrypt <входной_файл> <файл_ключа> <выходной_файл>" << endl;
        cout << endl;
        cout << "Файл ключа — бинарный файл длиной 16 байт (128 бит)." << endl;
        return 1;
    }

    string mode = argv[1];

    if (mode == "encrypt") {
        encryptFile(argv[2], argv[3], argv[4]);
    } else if (mode == "decrypt") {
        decryptFile(argv[2], argv[3], argv[4]);
    } else {
        cerr << "Ошибка: неизвестный режим '" << mode << "'. Используйте encrypt или decrypt." << endl;
        return 1;
    }

    return 0;
}
